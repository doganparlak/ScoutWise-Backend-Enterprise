"""Single-match attacking/defensive profiles using the shared style catalogue."""
from copy import deepcopy
from math import isfinite
import json

from report_module.profile_generation import generate_profile_json
from report_module.profile_evidence import bind_row_evidence, profile_snapshot, PROFILE_EVIDENCE_GUIDANCE
from report_module.terminology import FOOTBALL_TERMINOLOGY_GUIDANCE
from report_module.attacking_styles import select_team_styles

VERSION = 3
TASKS = {
    'attack': [('Goals',1.3,'finishing'),('Expected Goals',1,'finishing'),('Shots Total',1,'shots'),
               ('Key Passes',1.2,'creation'),('Expected Assists',1,'creation'),('Assists',1.1,'assists'),
               ('Successful Dribbles',1,'carrying'),('Passes In Final Third',.8,'progression'),
               ('Through Balls',.8,'progression'),('Accurate Crosses',.8,'width')],
    'defence': [('Tackles Won',1.2,'tackling'),('Tackles',1,'tackling'),('Interceptions',1.2,'interceptions'),
                ('Ball Recovery',1.2,'recoveries'),('Duels Won',1,'duels'),('Aerials Won',1,'aerials'),
                ('Clearances',.8,'clearances'),('Blocked Shots',1,'blocks')],
}
RATE_PAIRS = {'Shots Total':'Shots On Target (%)','Successful Dribbles':'Dribble Accuracy (%)',
              'Accurate Crosses':'Accurate Crosses (%)','Tackles Won':'Tackles Won (%)',
              'Duels Won':'Duels Won (%)','Aerials Won':'Aerials Won (%)'}


def _values(row):
    from coach_pool_module.profiles import METRIC_ALIASES
    result = {}
    for metric in [*[m for rows in (row.get('categories') or {}).values() for m in rows],*(row.get('expected_metrics') or [])]:
        if metric.get('_inferred_zero'):
            continue
        name = METRIC_ALIASES.get(metric.get('name'),metric.get('name'))
        value = metric.get('value')
        if value is None or isinstance(value,bool) or value == '':
            continue
        try:
            value = float(str(value).rstrip('%'))
        except (ValueError,TypeError):
            continue
        if name and isfinite(value) and value>=0 and ('%' not in name or value<=100):
            result[name] = value
    return result


def _cell(name,value):
    rate = '%' in name or 'percentage' in name.lower()
    shown = f'{value:.2f}'.rstrip('0').rstrip('.')
    return {'label':name,'value':('%' if rate else '')+shown,'basis':'rate' if rate else 'perMatch','contributes':True}


def select_players(report,team_id,side):
    players = []
    for row in report.get('lineups') or []:
        if int(row.get('team_id') or 0)!=int(team_id):
            continue
        position = str(row.get('position_name') or '').casefold()
        if row.get('position_id')==24 or 'goalkeeper' in position or 'kaleci' in position:
            continue
        values = _values(row)
        if values.get('Minutes Played',0)<=0:
            continue
        players.append((row,values))
    totals = {name:sum(values.get(name,0) for _,values in players) for name,_,_ in TASKS[side]}
    ranked = []
    for row,values in players:
        signals = [(name,weight*values[name]/totals[name],family) for name,weight,family in TASKS[side]
                   if values.get(name,0)>0 and totals[name]>0]
        signals.sort(key=lambda item:(-item[1],item[0]))
        families, chosen = set(), []
        for name,_,family in signals:
            if family not in families:
                chosen.append(name); families.add(family)
            if len(chosen)==4:
                break
        # A relevant success rate can complete a player's count/rate picture.
        if len(chosen)<4:
            chosen += [RATE_PAIRS[name] for name in chosen if name in RATE_PAIRS and RATE_PAIRS[name] in values][:4-len(chosen)]
        if len(chosen)<3:
            continue
        score = sum(max(s[1] for s in signals if s[2]==family) for family in families)
        source = {name:_cell(name,values[name]) for name in chosen}
        ranked.append((score,{'playerId':row['player_id'],'name':row.get('player_name') or '',
                             'imageUrl':row.get('player_image_url'),'position':row.get('position_name') or '',
                             'minutes':values['Minutes Played'],'averageRating':values.get('Rating',0),
                             'source_metrics':source}))
    ranked.sort(key=lambda item:(-item[0],-item[1]['minutes'],str(item[1]['playerId'])))
    return [player for _,player in ranked[:2]]


def _paragraphs(row,count):
    import re
    value = row.get('analysis')
    values = [value] if isinstance(value,str) else value if isinstance(value,list) else []
    analysis = [p.strip() for value in values if isinstance(value,str) for p in re.split(r'\n\s*\n',value) if p.strip()]
    row['analysis'] = analysis
    counts = [len(p.split()) for p in analysis]
    if len(analysis)!=count or any(not 20<=words<=35 for words in counts):
        raise ValueError(f"{row.get('styleId') or row.get('playerId')}: expected {count} paragraphs of 20-35 words, got {counts}")


def _bind_profile(draft,plan):
    result = {'themes':[],'players':[]}
    for style in plan['themes']:
        row = next((r for r in draft.get('themes') or [] if r.get('styleId')==style['styleId']),None)
        if row is None:
            raise ValueError('Missing selected match style')
        _paragraphs(row,2)
        source = style['source_metrics']
        bound = bind_row_evidence({**row,**{k:style[k] for k in ('styleId','styleSide','title','description')}},source,4)
        if bound.get('evidence_validation'):
            raise ValueError(f"Style {style['styleId']}: {bound['evidence_validation']}; allowed metrics: {list(source)}")
        bound['metrics']=list(deepcopy(source).values());bound['evidence_values']=deepcopy(source)
        result['themes'].append(bound)
    for player in plan['players']:
        row = next((r for r in draft.get('players') or [] if str(r.get('playerId'))==str(player['playerId'])),None)
        if row is None:
            raise ValueError('Missing selected match player')
        _paragraphs(row,1)
        source = player['source_metrics']
        bound = bind_row_evidence({**row,**{k:v for k,v in player.items() if k!='source_metrics'}},source,4)
        if bound.get('evidence_validation'):
            raise ValueError(f"Player {player['playerId']}: {bound['evidence_validation']}; allowed metrics: {list(source)}")
        bound['metrics']=list(deepcopy(source).values());bound['evidence_values']=deepcopy(source)
        result['players'].append(bound)
    return profile_snapshot(result)


def _repair_draft(draft,plans,lang):
    fixes, targets = [], {}
    for plan in plans:
        team = next((r for r in draft.get('themes') or [] if str(r.get('teamId'))==str(plan['teamId'])),None)
        if team is None:
            team={'teamId':plan['teamId']};draft.setdefault('themes',[]).append(team)
        for field in ('attackProfile','defenseProfile'):
            profile=team.setdefault(field,{})
            for kind,identity,count in [('themes','styleId',2),('players','playerId',1)]:
                for source in plan[field][kind]:
                    rows=profile.setdefault(kind,[])
                    row=next((r for r in rows if str(r.get(identity))==str(source[identity])),None)
                    if row is None:
                        row={identity:source[identity]};rows.append(row)
                    try:
                        _paragraphs(row,count)
                        bound=bind_row_evidence(row,source['source_metrics'],4)
                        if bound.get('evidence_validation'):
                            raise ValueError('; '.join(bound['evidence_validation']))
                    except ValueError as exc:
                        key=f"{plan['teamId']}:{field}:{kind}:{source[identity]}"
                        targets[key]=row
                        fixes.append({'rowId':key,'team':plan['teamName'],'identity':source.get('title') or source.get('name'),
                                      'paragraphs':count,'original':row,'reason':str(exc),'source_metrics':source['source_metrics'],
                                      'readingBoundary':source.get('readingBoundary')})
    if not fixes:
        raise ValueError('Unable to locate invalid match profile passage')
    repair=generate_profile_json([
        ('system',"Repair ONLY the supplied match-profile rows. Return JSON with themes as an array of {rowId, analysis, evidence_metrics}. Copy rowId exactly. analysis is an array of the requested paragraph count, each ONE paragraph of 20-30 words, aiming for 25. evidence_metrics is an aligned array of arrays of exact names from that row's source_metrics. Keep supported football conclusions, repair the stated problem, never invent facts or copy another row's data. Single-match counts are actual totals, never per-90 or averages. Use natural requested-language football terminology. No markdown or embedded newlines."+FOOTBALL_TERMINOLOGY_GUIDANCE),
        ('human',json.dumps({'language':lang,'rows':fixes},ensure_ascii=False))])
    for corrected in repair.get('themes') or []:
        row=targets.get(corrected.get('rowId'))
        if row is not None:
            row.update({key:corrected[key] for key in ('analysis','evidence_metrics') if key in corrected})
    return draft


def build_match_profiles(report,lang='en'):
    plans = []
    for team in report.get('teams') or []:
        sides = {}
        for side,field in [('attack','attackProfile'),('defence','defenseProfile')]:
            styles = select_team_styles([report],int(team['id']),lang,side,single_match=True)
            sides[field]={'themes':[{k:style[k] for k in ('styleId','styleSide','title','description','source_metrics','readingBoundary')} for style in styles],
                          'players':select_players(report,team['id'],side)}
        plans.append({'teamId':team['id'],'teamName':team.get('name'),**sides})
    if not any(plan[field][kind] for plan in plans for field in ('attackProfile','defenseProfile') for kind in ('themes','players')):
        return {'version':VERSION,'teams':[{'team_id':plan['teamId'],'attackProfile':{'themes':[],'players':[]},'defenseProfile':{'themes':[],'players':[]}} for plan in plans]}
    messages = [
        ('system',"You are ScoutWise Enterprise's senior match analyst. Return JSON with themes as the root array: one object per supplied teamId, containing teamId, attackProfile and defenseProfile. Each profile contains themes and players. Copy exactly the supplied selected styleIds and playerIds in source order, never add or replace a selection. Use ONLY this SINGLE MATCH, never a team season or a recent-match sample. Every theme returns styleId, analysis as exactly TWO strings of 25 words EACH (20-30 words, minimum 20 and maximum 35), and evidence_metrics as two aligned arrays of exact source metric names. First paragraph explains the measured attacking/defensive dimension in this match; second adds a distinct supported football implication. Every player returns playerId, analysis as exactly ONE string in an array, 25 words (20-30 words, minimum 20 and maximum 35), and evidence_metrics as one aligned array. Explain the player's concrete contribution to this team's attacking or defensive performance in this match. Evidence must come from that row's source_metrics only. Every paragraph should connect at least two relevant metrics; do not recite a list of values. At most one short numerical fact per paragraph. All counts are actual match totals; NEVER say per 90 or per match averages. Percentages and per-100-pass rates retain their own meanings. Role contribution shares compare outfield players doing the SAME task. Player totals belong to the player; team/opponent totals never become player statistics. Selected styles are statistical fits, not directly observed pressing timing, off-ball positioning or coaching intent. Obey each readingBoundary. No invented causes, movement, tactical schemes, benchmarks, recommendations or missing-data explanations. Do not claim recurring or habitual styles from one match. In Turkish use dripling, kilit pas, başarı oranı and saha oyuncuları. No markdown, bullets or embedded newline inside analysis strings."+FOOTBALL_TERMINOLOGY_GUIDANCE+PROFILE_EVIDENCE_GUIDANCE),
        ('human',json.dumps({'language':lang,'fixture':report.get('fixture'),'selected_teams':plans},ensure_ascii=False))]
    draft = generate_profile_json(messages)
    for attempt in range(2):
        try:
            teams=[]
            for plan in plans:
                row=next((r for r in draft['themes'] if str(r.get('teamId'))==str(plan['teamId'])),None)
                if row is None:
                    raise ValueError('Missing match team')
                bound_team={'team_id':plan['teamId'],**{field:_bind_profile(row.get(field) or {},plan[field]) for field in ('attackProfile','defenseProfile')}}
                previous=next((team for team in (report.get('match_profiles') or {}).get('teams',[]) if team.get('team_id')==plan['teamId']),{})
                # Adding a secondary dimension should not rewrite established
                # interpretations whose selected identity and evidence stayed fixed.
                for field in ('attackProfile','defenseProfile'):
                    for kind,identity in (('themes','styleId'),('players','playerId')):
                        for index,item in enumerate(bound_team[field][kind]):
                            old=next((saved for saved in previous.get(field,{}).get(kind,[]) if saved.get(identity)==item.get(identity) and saved.get('metrics')==item.get('metrics')),None)
                            if old:
                                bound_team[field][kind][index]=deepcopy(old)
                teams.append(bound_team)
            return {'version':VERSION,'teams':teams}
        except (ValueError,KeyError,TypeError) as exc:
            if attempt:
                raise
            draft = _repair_draft(draft,plans,lang)


def upgrade_match_profiles(content):
    if content.get('report_type')=='pre_match' or (content.get('match_profiles') or {}).get('version')==VERSION:
        return content
    try:
        return {**content,'match_profiles':build_match_profiles(content,content.get('language') or 'en')}
    except Exception as exc:
        print(f'[enterprise_match_report] event=match_profiles_deferred error_type={type(exc).__name__}')
        return content
