"""Model-selected tactical topics grounded in team and outfield-player evidence."""
import copy
import hashlib
import json
import math
import os
import re
from collections import defaultdict

from fastapi import Depends, HTTPException, Path
from sqlalchemy import text
from api_module.database import get_db
from api_module.utilities import require_auth
from match_report_module.report import TEAM_METRIC_CATEGORY, DERIVED_PERCENTAGE_METRICS
from . import analysis as history, comparison, tactics
from .progressive import NARRATIVE_LOCKS, progressive_response

VERSION = 17
PLAYER_METRICS = tactics.DETAIL_METRICS
ATTACK_PLAYER_METRICS = {'Passes','Key Passes','Shots Total','Successful Dribbles','Total Crosses','Passes In Final Third','Through Balls','Touches'}
DEFENCE_PLAYER_METRICS = {'Tackles','Interceptions','Ball Recovery','Clearances','Duels Won','Total Duels','Aerials Won','Fouls','Tackles Won'}
ROLES = {'defender':('Savunma hattı','Defenders'),'midfielder':('Orta saha','Midfielders'),'attacker':('Hücum hattı','Attackers')}
TEAM_METRICS = {'Counter Attacks','Ball Possession %','Passes','Accurate Passes','Long Balls','Long Balls Won','Key Passes','Total Crosses','Accurate Crosses','Dribble Attempts','Successful Dribbles','Shots Total','Shots On Target','Shots Insidebox','Shots Outsidebox','Goals','Big Chances Created','Big Chances Missed','Dangerous Attacks','Attacks','Interceptions','Tackles','Tackles Won','Ball Recovery','Blocked Shots','Fouls','Yellow Cards','Red Cards','Error Lead To Shot','Error Lead To Goal','Corners','Expected Goals','Expected Goals On Target','Expected Goals Open Play (xGOP)','Expected Goals Set Play (xGSP)'}
OPPONENT_METRICS={'Shots Total','Shots On Target','Shots Insidebox','Shots Outsidebox','Key Passes','Total Crosses','Accurate Crosses','Dribble Attempts','Successful Dribbles','Big Chances Created','Dangerous Attacks','Passes','Accurate Passes','Expected Goals'}
TEAM_METRICS |= {'Clearances','Total Duels','Duels Won','Aerials Won','Offsides','Offsides Provoked','Passes In Final Third','Through Balls','Through Balls Won','Backward Passes','Touches','Possession Lost','Dispossessed','Dribbled Past'}
OPPONENT_METRICS |= {'Counter Attacks','Offsides','Long Balls','Long Balls Won','Passes In Final Third','Through Balls','Total Duels','Duels Won','Possession Lost'}
METRIC_ALIASES={'Expected Goals (xG)':'Expected Goals','Expected Goals on Target (xGoT)':'Expected Goals On Target'}
FORBIDDEN=re.compile(r'goalkeep|save|kaleci|kurtar|punch|xgp|goal.prevent',re.I)


def add_counts(sample,key,value,meta):
    if value is not None and math.isfinite(value) and value>=0:sample[key]=(value,1);meta.setdefault(key,{'name':key,'percentage':False})


def extract_match(fixture,team_id,meta):
    opponent=next((int(p['id']) for p in fixture.get('participants') or [] if p.get('id') and int(p['id'])!=team_id),None)
    values={}
    for row in [*(fixture.get('statistics') or []),*(fixture.get('xgfixture') or fixture.get('xGFixture') or [])]:
        original=(row.get('type') or {}).get('name') or ''
        name=METRIC_ALIASES.get(original,TEAM_METRIC_CATEGORY.get(original,('',original))[1])
        value=tactics.numeric((row.get('data') or {}).get('value'))
        if value is not None and value>=0:values[(int(row.get('participant_id') or 0),name)]=value
    sample={}
    for name in TEAM_METRICS:add_counts(sample,name,values.get((team_id,name)),meta)
    for name in OPPONENT_METRICS:add_counts(sample,'Opponent '+name,values.get((opponent,name)),meta)
    pairs={**DERIVED_PERCENTAGE_METRICS,'Long Ball Share (%)':('Long Balls','Passes'),'Inside-box Shot Share (%)':('Shots Insidebox','Shots Total'),'Cross Share (%)':('Total Crosses','Passes'),'Final-third Pass Share (%)':('Passes In Final Third','Passes')}
    for name,(a,b) in pairs.items():
        for prefix in ('','Opponent '):
            aa,bb=sample.get(prefix+a),sample.get(prefix+b)
            if aa is not None and bb is not None and bb[0]>0 and 0<=aa[0]<=bb[0]:
                sample[prefix+name]=(aa[0],bb[0]);meta[prefix+name]={'name':prefix+name,'percentage':True}
    # Preserve a recorded provider percentage when its paired counts are absent.
    for name,(numerator,denominator) in DERIVED_PERCENTAGE_METRICS.items():
        for prefix in ('','Opponent '):
            key=prefix+name;value=values.get((opponent if prefix else team_id,name))
            if prefix+numerator in sample and prefix+denominator in sample:
                continue
            if key not in sample and value is not None and 0<=value<=100:
                sample[key]=(value,100);meta[key]={'name':key,'percentage':True}
    add_context_rates(sample, meta)
    if 'Ball Possession %' in sample:meta['Ball Possession %']['percentage']=True
    return sample


def add_context_rates(sample, meta):
    def rate(name, numerator, denominator, scale=1, percentage=False):
        a, b = sample.get(numerator), sample.get(denominator)
        if a is not None and b is not None and b[0] > 0 and (not percentage or 0 <= a[0] <= b[0]):
            sample[name] = (a[0] * scale, b[0])
            meta[name] = {'name':name, 'percentage':percentage}
    rate('Shots Per 100 Passes', 'Shots Total', 'Passes', 100)
    rate('Outfield Final-third Pass Share (%)', 'Outfield Passes In Final Third', 'Outfield Passes', percentage=True)
    rate('Outfield Duels Won (%)', 'Outfield Duels Won', 'Outfield Total Duels', percentage=True)
    rate('Recovery To Loss Ratio', 'Ball Recovery', 'Possession Lost')
    for label, source in [('Tackles','Tackles'),('Interceptions','Interceptions'),('Recoveries','Ball Recovery'),('Duels','Total Duels'),('Fouls','Fouls')]:
        rate(f'{label} Per 100 Opponent Passes', source, 'Opponent Passes', 100)
        rate(f'Outfield {label} Per 100 Opponent Passes', 'Outfield '+source, 'Opponent Passes', 100)
    rate('Outfield Recovery To Loss Ratio', 'Outfield Ball Recovery', 'Outfield Possession Lost')


class PlayerSamples:
    """Accumulate outfield contributions without retaining lineup payloads."""
    def __init__(self, team_id, meta):
        self.team_id = team_id
        self.meta = meta
        self.samples = {}
        self.players = {}

    def add(self, fixture):
        team_id, meta, players = self.team_id, self.meta, self.players
        rows=[p for p in fixture['players'] if p['teamId']==team_id and tactics.role_name(p)!='goalkeeper']
        totals=defaultdict(float);grouped=defaultdict(lambda:defaultdict(float))
        for player in rows:
            role=tactics.role_name(player)
            record=players.setdefault(player['id'],{'id':player['id'],'name':player['name'],'role':role,'minutes':0,'matches':0,'roleMinutes':defaultdict(float),'totals':defaultdict(float),'coverage':defaultdict(int)})
            record['minutes']+=player['minutes'];record['matches']+=1;record['roleMinutes'][role]+=player['minutes']
            for metric,value in player['metrics'].items():
                if value<0:continue
                record['totals'][metric]+=value;record['coverage'][metric]+=1
                totals[metric]+=value;grouped[role][metric]+=value
        sample={}
        for role,names in grouped.items():
            if role not in ROLES:continue
            for metric,value in names.items():
                if totals[metric]>0:
                    key=f'role:{role}:{metric}';sample[key]=(value,totals[metric])
                    meta[key]={'name':f'{ROLES[role][1]} {metric} Share (%)','percentage':True,'role':role,'playerMetric':metric}
        # Record observed outfield totals separately from provider team statistics.
        for metric, total in totals.items():
            key='Outfield '+metric;sample[key]=(total,1)
            meta[key]={'name':key,'percentage':False}
        concentrations=[]
        for metric in ('Key Passes','Successful Dribbles'):
            values=[p['metrics'][metric] for p in rows if metric in p['metrics']]
            if values and sum(values)>0:concentrations.append(max(values)/sum(values))
        if concentrations:
            key='Creator Concentration (%)';sample[key]=(sum(concentrations),len(concentrations))
            meta[key]={'name':key,'percentage':True}
        balances=[]
        for metric in ('Interceptions','Ball Recovery'):
            if all(metric in grouped[role] for role in ROLES):
                values=[grouped[role][metric] for role in ROLES];total=sum(values)
                if total>0:balances.append(-sum(v/total*math.log(v/total) for v in values if v>0)/math.log(3))
        if balances:
            key='Defensive Role Balance (%)';sample[key]=(sum(balances),len(balances))
            meta[key]={'name':key,'percentage':True}
        self.samples[fixture['id']]=sample

    def result(self):
        for player in self.players.values():
            player['role'] = max(player['roleMinutes'], key=player['roleMinutes'].get)
        return self.samples, list(self.players.values())


def player_samples(details,team_id,meta):
    accumulator = PlayerSamples(team_id, meta)
    for fixture in details:
        accumulator.add(fixture)
    return accumulator.result()


def pool(samples,keys,meta):
    return [{'key':key,**meta[key],'value':round(sum(s[key][0] for s in samples)/sum(s[key][1] for s in samples)*(100 if meta[key]['percentage'] and key!='Ball Possession %' else 1),3)} for key in keys]


def build_evidence(db,periods,groups,coach_id):
    from .evidence import load_period
    meta={};seasons=[]
    for p in periods:
        ids=sorted({fid for row in groups[p['id']] for fid in row['fixtureIds']})
        shared=load_period(db,coach_id,p['teamId'],ids)
        meta.update(shared['meta'])
        samples = [{} for _ in range(shared['sampleCount'])]
        for metric, values in shared['columns'].items():
            for sample, value in zip(samples, values):
                if value is not None:
                    sample[metric] = tuple(value) if isinstance(value, (list, tuple)) else (value, 1)
        for sample in samples:
            add_context_rates(sample, meta)
        seasons.append({'period':p,'samples':samples,'players':shared['players']})
    catalog={}
    for key,definition in meta.items():
        if FORBIDDEN.search(key):continue
        rows=[]
        for season in seasons:
            samples=[s for s in season['samples'] if key in s]
            if len(samples)<max(5,math.ceil(season['period']['matchCount']*.7)):continue
            metric=pool(samples,[key],meta)[0]
            observations=[s[key][0]/s[key][1]*(100 if definition['percentage'] and key!='Ball Possession %' else 1) for s in samples]
            mean=sum(observations)/len(observations)
            variation=(sum((x-mean)**2 for x in observations)/len(observations))**.5/max(abs(mean),.001)
            rows.append({'periodId':season['period']['id'],'value':metric['value'],'covered':len(samples),'matches':season['period']['matchCount'],
                'matchVariation':round(variation,3),'range':[round(min(observations),2),round(max(observations),2)]})
        if rows:catalog[key]={**definition,'seasons':rows}
    return meta,seasons,catalog


def select_leaders(players,metric_names,limit=2):
    names=[n for n in metric_names if n in PLAYER_METRICS]
    totals={name:sum(p['totals'].get(name,0) for p in players) for name in names}
    names=[name for name in names if totals[name]>0]
    if not names:return []
    ranked=[]
    for player in players:
        shares=[player['totals'].get(name,0)/totals[name] for name in names]
        score=sum(shares)/len(shares)*100
        if score<=0:continue
        ranked.append({'id':player['id'],'name':player['name'],'role':player['role'],'minutes':round(player['minutes']),
            'matches':player['matches'],'contributionScore':round(score,2),
            'metrics':[{'name':name,'total':round(player['totals'].get(name,0),2),'share':round(player['totals'].get(name,0)/totals[name]*100,2),
                        'covered':player['coverage'].get(name,0)} for name in names]})
    result=sorted(ranked,key=lambda p:(-p['contributionScore'],-p['minutes'],p['id']))
    return result if limit is None else result[:limit]


def topic_side_valid(keys,player_keys,side):
    attack={'Passes','Accurate Passes','Accurate Passes (%)','Long Balls','Long Balls Won','Long Ball Share (%)','Long Balls Won (%)','Key Passes','Total Crosses','Accurate Crosses','Accurate Crosses (%)','Dribble Attempts','Successful Dribbles','Dribble Accuracy (%)','Shots Total','Shots On Target','Shots On Target (%)','Shots Insidebox','Shots Outsidebox','Inside-box Shot Share (%)','Goals','Goal Conversion (%)','Big Chances Created','Big Chances Missed','Dangerous Attacks','Attacks','Corners'}
    defensive={'Tackles','Tackles Won','Interceptions','Ball Recovery','Blocked Shots','Fouls','Clearances','Total Duels','Duels Won','Aerials Won','Outfield Duels Won (%)','Defensive Role Balance (%)','Recovery To Loss Ratio','Yellow Cards','Red Cards','Error Lead To Shot','Error Lead To Goal'}
    attack |= {'Counter Attacks','Passes In Final Third','Through Balls','Touches','Final-third Pass Share (%)','Cross Share (%)','Shots Per 100 Passes','Creator Concentration (%)','Outfield Final-third Pass Share (%)'}
    if side=='attack':
        if any(key not in ATTACK_PLAYER_METRICS for key in player_keys):return False
        valid=lambda key:key in attack or key.startswith('Expected Goals') or key.startswith('role:') and key.split(':',2)[-1] in attack
        return sum(bool(valid(k)) for k in keys)>=2 and any(k in ATTACK_PLAYER_METRICS for k in player_keys)
    valid=lambda key:key.startswith('Opponent ') or key in defensive or 'Per 100 Opponent Passes' in key or key.startswith('Outfield ') and key[9:] in defensive or key.startswith('role:') and key.split(':',2)[-1] in defensive
    return sum(bool(valid(k)) for k in keys)>=2 and any(k in DEFENCE_PLAYER_METRICS for k in player_keys)


def materialize(plan,seasons,meta,side):
    keys=plan.get('metrics',[])
    if not isinstance(keys,list) or not 3<=len(keys)<=4 or len(set(keys))!=len(keys) or any(k not in meta or FORBIDDEN.search(k) for k in keys):return None
    primary=plan.get('primaryMetric')
    if primary not in keys:return None
    player_keys=plan.get('playerMetrics',[])
    if not isinstance(player_keys,list) or not 1<=len(player_keys)<=3 or any(k not in PLAYER_METRICS or FORBIDDEN.search(k) for k in player_keys):return None
    if not topic_side_valid(keys,player_keys,side):return None
    rows=[];players=[];sample_values=[]
    for season in seasons:
        p=season['period'];matched=[sample for sample in season['samples'] if all(k in sample for k in keys)]
        if len(matched)<max(5,math.ceil(p['matchCount']*.6)):continue
        metrics=pool(matched,keys,meta)
        rows.append({'periodId':p['id'],'team':p['team'],'season':p['season'],'teamImageUrl':p['teamImageUrl'],
            'matches':p['matchCount'],'covered':len(matched),'supports':True,'metrics':metrics})
        players.append({'periodId':p['id'],'team':p['team'],'season':p['season'],'teamImageUrl':p['teamImageUrl'],
            'players':select_leaders(season['players'],player_keys)})
        if len(seasons)==1:
            sample_values=[round(s[primary][0]/s[primary][1]*(100 if meta[primary]['percentage'] and primary!='Ball Possession %' else 1),3) for s in matched]
    if not rows:return None
    kind='common'
    if len(seasons)==1:
        mean=sum(sample_values)/max(len(sample_values),1)
        variation=(sum((v-mean)**2 for v in sample_values)/max(len(sample_values),1))**.5/max(abs(mean),.001)
        kind='consistent' if variation<=.5 else 'single'
    values=[next(m['value'] for m in row['metrics'] if m['key']==primary) for row in rows]
    # Flag real differences without claiming a league benchmark or a universal tactic.
    if len(values)>1 and max(values)-min(values)>max(10 if meta[primary]['percentage'] else .5,abs(sum(values)/len(values))*.4):kind='difference'
    if len(rows)<math.ceil(len(seasons)*2/3):kind='subset'
    return {'id':plan.get('id') or primary,'kind':kind,'rows':rows,'players':players,'samples':sample_values,
        'primaryMetric':{'key':primary,**meta[primary]},'supportingSeasons':len(rows),'availableSeasons':len(rows),'selectedSeasons':len(seasons),
        'metricKeys':keys,'playerMetricKeys':player_keys}


def valid_text(text,limit):
    return isinstance(text,str) and 0<len(text.split())<=limit and not re.search(r'\d|%|sınırlı|atıf|kaleci|kurtar|goalkeep|save|yüksek pres|high press|half.space|iç koridor|patlama|explosion|azalırken|artarken|giderek|stoper|full.?back|centre.?back',text,re.I)


def fallback_explanation(topic,meta,lang):
    role_keys=[key for key in topic['metricKeys'] if meta[key].get('role')]
    labels={'Passes':('pas dolaşımı','passing circulation'),'Key Passes':('pozisyon hazırlığı','chance creation'),
        'Shots Total':('şut üretimi','shot production'),'Successful Dribbles':('bire bir katkısı','dribbling'),
        'Total Crosses':('orta üretimi','crossing'),'Tackles':('müdahale yükü','tackling workload'),
        'Interceptions':('pas kesme katkısı','interceptions'),'Ball Recovery':('top kazanımı','ball recovery'),
        'Clearances':('tehlikeyi uzaklaştırma','clearances'),'Total Duels':('ikili mücadele yükü','duel workload'),'Duels Won':('kazanılan ikili mücadeleler','duels won'),'Aerials Won':('hava topu kazanımı','aerial duels won'),'Fouls':('faul yükü','foul workload'),'Tackles Won':('başarılı müdahaleler','tackles won'),
        'Passes In Final Third':('son bölge pasları','final-third passing'),'Through Balls':('ara pas üretimi','through-ball creation'),'Touches':('topla buluşma','touches')}
    if role_keys:
        key=role_keys[0];definition=meta[key];role=definition['role'];metric=definition['playerMetric']
        task=labels[metric][0 if lang=='tr' else 1]
        role_label=ROLES[role][0 if lang=='tr' else 1]
        peers=[other for other in role_keys[1:] if meta[other]['playerMetric']==metric]
        if peers:
            other=peers[0];differences=[next(m['value'] for m in row['metrics'] if m['key']==key)-next(m['value'] for m in row['metrics'] if m['key']==other) for row in topic['rows']]
            if any(v>0 for v in differences) and any(v<0 for v in differences):
                first=f'{task.capitalize()} sorumluluğunun mevki grupları arasındaki dağılımı, seçili takım dönemlerinde farklı yönlere ayrılıyor.' if lang=='tr' else f'The positional distribution of {task} varies across the selected team periods.'
            else:
                leading=role if sum(differences)>=0 else meta[other]['role'];role_label=ROLES[leading][0 if lang=='tr' else 1]
                first=f'{role_label}, {task} sorumluluğunda daha fazla pay alarak takımın bu oyun yükünü taşıyan hattı oluşturuyor.' if lang=='tr' else f'{role_label} carry the larger share of {task}, identifying where this part of the team workload is concentrated.'
        else:
            first=f'{role_label}, {task} içindeki payıyla takımın bu oyun sorumluluğunda görünür bir rol üstleniyor.' if lang=='tr' else f'{role_label} have a visible role in {task} through their share of the recorded team contribution.'
        second=('Konunun öne çıkan oyuncuları, seçili maçlarda bu sorumluluğun sahadaki taşıyıcılarını ortaya koyuyor.' if topic['selectedSeasons']==1 else 'Konunun öne çıkan oyuncuları, farklı takım dönemlerinde bu sorumluluğun sahadaki taşıyıcılarını ortaya koyuyor.') if lang=='tr' else ('The leading contributors identify the players carrying this responsibility in the selected matches.' if topic['selectedSeasons']==1 else 'The leading contributors identify the players carrying this responsibility in the selected team periods.')
    else:
        first='Pas, hücum veya savunma aksiyonlarının birlikte dağılımı, konunun takım içindeki iş bölümünü belirginleştiriyor.' if lang=='tr' else 'The joint distribution of the selected actions describes how this workload is shared within the team.'
        second='Öne çıkan oyuncular, ilgili oyun sorumluluğunu en fazla üstlenen isimler olarak ayrışıyor.' if lang=='tr' else 'The leading contributors identify the players most involved in carrying that responsibility.'
    return first+' '+second


def mentions_player(text,seasons):
    if not isinstance(text,str):return False
    for season in seasons:
        for player in season['players']:
            name=player['name'].strip()
            common={'yüksek','high','long','short','white','brown','young','güçlü','strong','ball','cross','press','defender','midfielder','attacker'}
            tokens=[name]+[part for part in name.split() if len(part)>=5 and part.casefold() not in common]
            if any(token and re.search(r'(?<!\w)'+re.escape(token)+r'(?!\w)',text,re.I) for token in tokens):return True
    return False


def prepare_player_options(value,seasons):
    from itertools import combinations
    pools={season['period']['id']:season for season in seasons}
    value['playerPool']=[{'periodId':s['period']['id'],'team':s['period']['team'],'season':s['period']['season'],
        'players':[{key:p[key] for key in ('id','name','role','minutes','matches','totals','coverage')} for p in s['players']]} for s in seasons]
    seen_pairs=defaultdict(set)
    for side,topics in value['profiles'].items():
        used_metrics=set()
        for topic in topics:
            allowed=ATTACK_PLAYER_METRICS if side=='attack' else DEFENCE_PLAYER_METRICS
            names=[key for key in topic['playerMetricKeys'] if key in allowed]
            if not names:names=['Passes'] if side=='attack' else ['Ball Recovery','Interceptions']
            if frozenset(names) in used_metrics:
                relevant=[key.split(':',2)[-1] for key in topic['metricKeys'] if key.startswith('role:')]
                relevant += [key for key in topic['metricKeys'] if key in PLAYER_METRICS]
                for candidate in [list(dict.fromkeys(relevant))[:3],*[[key] for key in relevant]]:
                    candidate=[key for key in candidate if key in allowed and any(p['totals'].get(key,0)>0 for season in seasons for p in season['players'])]
                    if candidate and frozenset(candidate) not in used_metrics and topic_side_valid(topic['metricKeys'],candidate,side):
                        if any(select_leaders(s['players'],candidate) for s in seasons):names=candidate;break
            used_metrics.add(frozenset(names));topic['playerMetricKeys']=names
            for period in topic['players']:
                ranked=select_leaders(pools[period['periodId']]['players'],names,limit=None)
                pairs=list(combinations(ranked[:12],2))
                pairs.sort(key=lambda pair:-(pair[0]['contributionScore']+pair[1]['contributionScore']))
                best=next((pair for pair in pairs if tuple(sorted(p['id'] for p in pair)) not in seen_pairs[period['periodId']]),pairs[0] if pairs else tuple(ranked[:2]))
                selected=list(best);seen_pairs[period['periodId']].add(tuple(sorted(p['id'] for p in selected)))
                period['players']=selected
                # Pool stores identities/stats once; per-topic options contain only rank and score.
                period['options']=[{'id':p['id'],'contributionScore':p['contributionScore']} for p in ranked]


def hydrate_player_images(db,value):
    players=[player for topics in value['profiles'].values() for topic in topics for period in topic.get('players',[]) for player in period['players']]
    pool_players=[player for period in value.get('playerPool',[]) for player in period['players']]
    ids=sorted({player['id'] for player in players+pool_players})
    if not ids:return
    rows=db.execute(text("SELECT player_id,image_url FROM enterprise_player_images WHERE player_id=ANY(CAST(:ids AS bigint[])) AND image_status='available'"),{'ids':ids}).mappings().all()
    images={int(row['player_id']):row['image_url'] for row in rows}
    for player in players+pool_players:player['imageUrl']=images.get(player['id'])


def build_profiles(db, coach_id, data, ids, include_ai=True):
    groups = tactics.resolve_periods(data, ids)
    signature = [[key, sorted({fid for p in rows for fid in p['fixtureIds']})] for key, rows in sorted(groups.items())]
    key = 'profiles:' + hashlib.sha256(json.dumps([VERSION, coach_id, signature, include_ai]).encode()).hexdigest()
    cached = comparison.read_cache(db, key)
    if cached is not None:
        return cached
    # No raw fixture or lineup arrays are retained during generation; evidence is compact/cached.
    base = build_profiles(db, coach_id, data, ids, include_ai=False) if include_ai else None
    identity = tactics.build_tactics(db, coach_id, data, ids, include_ai=False)
    locks = NARRATIVE_LOCKS if include_ai else comparison._locks
    with locks[int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(locks)]:
        cached = comparison.read_cache(db, key)
        if cached is not None:
            return cached
        meta, seasons, catalog = build_evidence(db, identity['periods'], groups, coach_id)
        from .archetypes import generate
        if include_ai:
            if db is not None:
                db.rollback()  # AI must not occupy a database connection while other pages load.
            generated = generate(seasons, meta, catalog, {}, fixed_topics=base['profiles'])
            value = copy.deepcopy(base)
            # AI adds prose only; labels, players and deterministic evidence stay stable.
            for side, topics in value['profiles'].items():
                summaries = {topic['id']:topic['summary'] for topic in generated[side]}
                for topic in topics:
                    topic['summary'] = summaries.get(topic['id'], topic['summary'])
        else:
            value = {'periods':[{k:p[k] for k in ('id','team','teamId','season','teamImageUrl','matchCount','competitions')} for p in identity['periods']],
                'profiles':generate(seasons, meta, catalog, {}, allow_ai=False),
                'wordBudgets':{'tr':90, 'en':90}}
            prepare_player_options(value, seasons)
            hydrate_player_images(db, value)
        comparison.write_cache(db, key, value, 6)
        return value


@history.router.post('/coach-analysis/{coach_id}/profiles')
def coach_profiles(body:tactics.TacticsRequest, coach_id:int=Path(gt=0), user_id:str=Depends(require_auth), db=Depends(get_db)):
    try:
        data = history.get_periods(db, coach_id)
        if body.stream:
            first = build_profiles(db, coach_id, data, body.periodIds, include_ai=False)
            return progressive_response(first, lambda: build_profiles(db, coach_id, data, body.periodIds))
        return build_profiles(db, coach_id, data, body.periodIds)
    except comparison.SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None
