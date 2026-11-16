"""Fixed, multi-metric performance findings over the original selected matches.

Comparisons use matched own/opponent observations or explicit opportunity/outcome
relationships. No league benchmarks, missing-as-zero values, or arbitrary style
scores are used to declare strengths or weaknesses.
"""
from copy import deepcopy
from math import ceil, isfinite
from report_module.profile_evidence import bind_row_evidence, PROFILE_EVIDENCE_GUIDANCE
from report_module.profile_generation import generate_profile_json
from report_module.terminology import FOOTBALL_TERMINOLOGY_GUIDANCE
import json
import os
import re

CATALOG_VERSION = 2
NARRATIVE_VERSION = 2
NARRATIVE_CONTRACT = """For strengths and weaknesses ONLY: keep exactly two paragraphs per finding,
targeting 30-38 words EACH (roughly 75% of the previous 40-50-word paragraphs).
Paragraph 1 identifies this team's actual football advantage or failure mechanism
through a relationship between at least two cited metrics. Paragraph 2 develops
a different, evidence-supported practical payoff, vulnerability or dependency.
Do not repeat the first conclusion, define the topic, narrate metric comparisons,
repeat the heading/description, add generic football platitudes, or open with a
statistical inventory. Explain WHY the observed relationship matters in this
specific team's selected analysis. Interpret supported implications confidently
without inventing tactics, roles, timing, locations, causation or future results.
Use dripling (never çalım or top sürme) in Turkish. Avoid empty conclusions such as
pratik değerini artırıyor or hücum planının değerini artırıyor; name the actual
football payoff or cost. Keep at most one short numerical fact per paragraph; the cells already show the
numbers. Findings evaluate the full selected sample; never select favorable or
unfavorable matches to establish a topic. Where source data has incomplete
coverage, keep the interpretation within that coverage and do not call a
covered-match value the overall selected-match average. Do not expand these passages to the generic 40-50-word
team-paragraph target during review or repair. All other sections keep their
existing editorial contracts."""

INTERPRETATION_FOCUS = {
 'creation': 'Both key-pass and clear-chance comparisons MUST shape the interpretation. Reveal whether this team turns preparation into genuinely dangerous opportunities, rather than merely accumulating preparatory actions. Do not infer extra routes, player options or creativity from key passes alone. Separate supply of clear opportunities from their finishing. Paragraph 1 concerns how final-pass preparation is accompanied by clear openings; paragraph 2 concerns the competitive burden of defending more clear openings, not the same preparation-to-chance statement again. Do not claim each key pass produced a clear chance.',
 'finishing': 'Connect overall shot conversion and conversion AFTER a shot reaches the target. Identify where the finishing edge or waste actually resides, rather than saying both rates are higher/lower. The opponent is only a finishing comparator: never credit this team with defending or stopping opposition shots from these finishing rates. Use goal/xG evidence only when actually supplied.',
 'passing': 'Explain the ball-use security implied by accuracy at the recorded passing load. Volume alone does not prove control, and accuracy does not prove penetration or resistance to a press.',
 'progression': 'Explain whether circulation is meaningfully reaching the final third, using the relationship between advanced-pass share and advanced-pass output. Do not invent passing lanes or movement.',
 'dribbling': 'Read success alongside successful-action output to identify the value or cost of individual attempts. Do not invent line-breaking, pitch locations or specific player dependencies.',
 'set_pieces': 'Identify the threat provided by set pieces in relation to recorded opportunities. Distinguish generating an additional source of chances from merely collecting corners; total set-play xG cannot be divided by corners.',
 'duels': 'Identify what success at the recorded contest workload contributes to this team, including aerial evidence where available. Do not turn action volume into physical dominance or infer positioning.',
 'recoveries': 'Explain this team’s capacity to interrupt opposing ball use through recoveries and interceptions relative to opponent passing activity. Never invent the timing of pressing or a direct transition-to-goal sequence.',
 'threat': 'Separate two dimensions: where the opponent’s attempts are concentrated, and how often its overall attempts reach the target. Explain the distinct exposure each reveals. Inside-box share and overall accuracy are separate marginal measurements: never claim the inside-box shots specifically were on target. Do not invent xG, goal probability, shot volume or a failed positional assignment.',
 'errors': 'Explain whether errors are becoming shots or goals and the distinct defensive exposure that creates. Separate preventing avoidable danger from general defensive activity; never call the team flawless.',
 'discipline': 'Connect fouls adjusted for opposition passing with card load per match to identify a measured disciplinary cost or restraint. These use different bases: do not calculate a cards-per-foul conversion. Do not invent restricted player choices, sanctions not recorded, emotions, foul locations, pressing schemes or points lost.',
 'game_management': 'Paragraph 1 interprets turning leads into wins; paragraph 2 MUST interpret restoring parity after trailing when those observations are supplied; otherwise explain a distinct implication of the recorded lead outcomes without inventing trailing evidence. Explain the distinct winning platform and response capacity these support. Never repeat lead management in both paragraphs, imply an uninterrupted lead, or invent coaching decisions or tactical adjustments.',
}
# Each identity has one distinct football scope and its two presentation labels.
CATALOG = {
 'creation': {'strength':('Pozisyon Üretim Gücü','Chance Creation Strength'), 'weakness':('Pozisyon Üretiminde Yetersizlik','Insufficient Chance Creation'), 'description':('Kilit paslar ve net fırsatlar üzerinden pozisyon üretiminin sürekliliği ve rakibe göre dengesi.','Chance creation through key passes and clear opportunities, compared with the opposition.')},
 'finishing': {'strength':('Bitiricilik Verimi','Finishing Efficiency'), 'weakness':('Bitiricilik Sorunu','Finishing Problems'), 'description':('Üretilen fırsatların kalitesi ve şutların gole dönüşümü arasındaki ilişki.','How chance quality and shot conversion translate into goals.')},
 'passing': {'strength':('Pas Oyununda Güven','Passing Security'), 'weakness':('Pas Oyununda Verim Kaybı','Inefficient Passing'), 'description':('Pas hacmi ve pas başarı oranının birlikte ortaya koyduğu top kullanma güvenliği.','Ball-use security through the relationship between passing volume and accuracy.')},
 'progression': {'strength':('Son Bölgeye Erişim Gücü','Final-third Access'), 'weakness':('Son Bölgeye Erişim Sorunu','Limited Final-third Access'), 'description':('Pas oyununun son bölgeye taşınması ve ileri bölgelerde bağlantı kurulması.','How passing reaches the final third and connects play in advanced areas.')},
 'dribbling': {'strength':('Dripling Başarısı','Dribbling Success'), 'weakness':('Driplinglerde Verimsizlik','Inefficient Dribbling'), 'description':('Bireysel girişimlerin başarı oranı ve hücuma sağladığı başarılı aksiyon katkısı.','The success rate of individual attempts and their successful attacking contribution.')},
 'set_pieces': {'strength':('Duran Top Tehdidi','Set-piece Threat'), 'weakness':('Duran Toplarda Üretim Sorunu','Limited Set-piece Production'), 'description':('Duran top fırsatları ile bu fırsatlardan üretilen gol beklentisinin dengesi.','The relationship between set-piece opportunities and the expected goals produced.')},
 'duels': {'strength':('İkili Mücadele Gücü','Duel Strength'), 'weakness':('İkili Mücadele Zafiyeti','Duel Vulnerability'), 'description':('Mücadele yükü, kazanma oranı ve mümkün olduğunda hava topu katkısının birlikte değerlendirilmesi.','Duel workload and success, with aerial contribution where recorded.')},
 'recoveries': {'strength':('Top Kazanım Etkinliği','Effective Ball Recovery'), 'weakness':('Top Kazanımında Yetersizlik','Insufficient Ball Recovery'), 'description':('Top kazanımı ve pas arası katkısının rakibin pas faaliyetine göre değerlendirilmesi.','Recoveries and interceptions assessed against the opposition’s passing activity.')},
 'threat': {'strength':('Rakip Tehdidini Sınırlama','Restricting Opposition Threat'), 'weakness':('Rakip Tehdidine Açıklık','Exposure to Opposition Threat'), 'description':('İzin verilen şut kalitesi ve isabet oranının aynı maçlardaki karşılıklı tehditle birlikte okunması.','Allowed shot quality and accuracy in the context of both sides’ threat in the same matches.')},
 'errors': {'strength':('Savunmada Hata Kontrolü','Defensive Error Control'), 'weakness':('Hata Kaynaklı Savunma Riski','Error-related Defensive Risk'), 'description':('Şut ve golle sonuçlanan kayıtlı hataların oluşturduğu savunma riskinin değerlendirilmesi.','Defensive risk from recorded errors leading to shots and goals.')},
 'discipline': {'strength':('Disiplinli Oyun','Disciplined Play'), 'weakness':('Disiplin Sorunları','Disciplinary Problems'), 'description':('Faul yükü ile sarı ve kırmızı kartların birlikte oluşturduğu disiplin profili.','Disciplinary performance through foul workload and yellow and red cards.')},
 'game_management': {'strength':('Skor Yönetimi','Game-state Management'), 'weakness':('Skor Yönetiminde Kırılganlık','Fragile Game-state Management'), 'description':('Öne geçilen maçları kazanma ve gerideyken eşitliği yeniden kurma kapasitesi.','Converting leads into wins and restoring parity after falling behind.')},
}

ALIASES = {'Expected Goals (xG)':'Expected Goals','Expected Goals on Target (xGoT)':'Expected Goals On Target'}
RATES = {
 'Accurate Passes (%)':('Accurate Passes','Passes'),
 'Goal Conversion (%)':('Goals','Shots Total'),
 'On Target Goal Conversion (%)':('Goals','Shots On Target'),
 'Inside-box Shot Share (%)':('Shots Insidebox','Shots Total'),
 'Outfield Duels Won (%)':('Outfield Duels Won','Outfield Total Duels'),
 'Outfield Final-third Pass Share (%)':('Outfield Passes In Final Third','Outfield Passes'),
 'Dribble Accuracy (%)':('Successful Dribbles','Dribble Attempts'),
 'Duels Won (%)':('Duels Won','Total Duels'),
 'Aerials Won (%)':('Aerials Won','Aerials'),
 'Shots On Target (%)':('Shots On Target','Shots Total'),
 'Shot Quality (%)':('Expected Goals','Shots Total'),
 'Final-third Pass Share (%)':('Passes In Final Third','Passes'),
}
ACTIVITY = {'Recoveries Per 100 Opponent Passes':'Ball Recovery',
            'Interceptions Per 100 Opponent Passes':'Interceptions',
            'Fouls Per 100 Opponent Passes':'Fouls',
            'Outfield Recoveries Per 100 Opponent Passes':'Outfield Ball Recovery',
            'Outfield Interceptions Per 100 Opponent Passes':'Outfield Interceptions'}


def number(value):
    if value is None or value == '' or isinstance(value, bool): return None
    try:
        value = float(value)
        return value if isfinite(value) and value >= 0 else None
    except (TypeError, ValueError): return None


def _values(team, lineups=()):
    from report_module.metric_validation import has_team_statistics, is_standard_count
    covered = has_team_statistics(team.get('categories') or {})
    result = {}
    for rows in [*(team.get('categories') or {}).values(), team.get('expected_metrics') or []]:
        for row in rows:
            name = ALIASES.get(row.get('name'), row.get('name'))
            value = number(row.get('value'))
            if name and value is not None and (not row.get('_inferred_zero') or covered and is_standard_count(name)) and ('%' not in name or value <= 100):
                result[name] = value
    # Recorded outfield contributions are a distinct, explicitly labelled fallback
    # when the provider has no corresponding team total. Never include keepers.
    totals, minutes_by_metric, total_minutes = {}, {}, 0
    for player in lineups:
        if int(player.get('team_id') or 0) != int(team.get('id') or 0): continue
        position = str(player.get('position_name') or '').lower()
        if player.get('position_id') == 24 or 'goalkeeper' in position or 'kaleci' in position: continue
        values = {metric.get('name'):number(metric.get('value')) for rows in (player.get('categories') or {}).values() for metric in rows}
        if not values.get('Minutes Played'): continue
        total_minutes += values['Minutes Played']
        for name in ('Total Duels','Duels Won','Ball Recovery','Interceptions','Passes','Passes In Final Third'):
            if values.get(name) is not None:
                totals[name] = totals.get(name,0)+values[name]
                minutes_by_metric[name] = minutes_by_metric.get(name,0)+values['Minutes Played']
    result.update({'Outfield '+name:value for name,value in totals.items() if total_minutes>0 and minutes_by_metric[name]/total_minutes>=.7})
    if 'Yellow Cards' in result and 'Red Cards' in result:
        result['Total Cards'] = result['Yellow Cards'] + result['Red Cards']
    return result


class Evidence:
    def __init__(self, reports, team_id):
        self.matches, seen = [], set()
        for index, report in enumerate(reports):
            fid = (report.get('fixture') or {}).get('id') or f'snapshot:{index}'
            if fid in seen: continue
            seen.add(fid)
            own = next((t for t in report.get('teams') or [] if int(t.get('id') or 0) == int(team_id)), None)
            opponents = [t for t in report.get('teams') or [] if int(t.get('id') or 0) != int(team_id)]
            if own is not None and len(opponents) == 1:
                self.matches.append((_values(own,report.get('lineups') or []), _values(opponents[0],report.get('lineups') or [])))
        self.minimum = max(min(3,len(self.matches)),ceil(len(self.matches)*.6))

    def observation(self, match, side, name):
        values, opponent = match[side], match[1-side]
        if name in RATES:
            a,b = RATES[name]
            if a in values and b in values:
                if values[b] > 0 and values[a] <= values[b]:
                    return values[a], values[b], 100
                return None  # No opportunity is not a zero success rate.
            if name in values: return values[name],1,1
            return None
        if name in ACTIVITY:
            a = ACTIVITY[name]
            if a in values and opponent.get('Passes',0)>0:
                return values[a],opponent['Passes'],100
            return None
        if name in values: return values[name],1,1
        return None

    def cell(self, name, side, observations):
        numerator = sum(row[0] for row in observations)
        denominator = sum(row[1] for row in observations)
        scale = observations[0][2]
        value = numerator/denominator*scale
        label = ('Opponent ' if side else '')+name
        rate = name in RATES
        shown = f'{value:.2f}'.rstrip('0').rstrip('.')
        return {'label':label,'value':f'%{shown}' if rate else shown,'contributes':True,
                'basis':'rate' if rate else 'per100OpponentPasses' if name in ACTIVITY else 'perMatch',
                'matchesCovered':len(observations), 'sampleSize':len(self.matches),
                'detail':f'{numerator:g} / {denominator:g}' if scale == 100 else '', '_number':value}

    def paired(self, name, inverse=False, weight=1):
        pairs = [(self.observation(m,0,name),self.observation(m,1,name)) for m in self.matches]
        pairs = [(a,b) for a,b in pairs if a is not None and b is not None and a[2]==b[2]]
        if name in RATES and pairs:
            counted=[p for p in pairs if p[0][2]==100]
            reported=[p for p in pairs if p[0][2]==1]
            pairs=counted if len(counted)>=self.minimum else reported
        if not pairs or len(pairs)<self.minimum: return None
        a,b = self.cell(name,0,[p[0] for p in pairs]),self.cell(name,1,[p[1] for p in pairs])
        denominator = abs(a['_number'])+abs(b['_number'])
        score = (a['_number']-b['_number'])/denominator if denominator else 0
        return {'score':-score if inverse else score,'weight':weight,'cells':[a,b],
                'comparison':'lower own value is preferable' if inverse else 'higher own value is preferable',
                'coverage':len(pairs)/len(self.matches)}

    def internal(self, first, second, weight=1, require_full=False):
        pairs = [(self.observation(m,0,first),self.observation(m,0,second)) for m in self.matches]
        pairs = [(a,b) for a,b in pairs if a is not None and b is not None]
        if not pairs or len(pairs)<self.minimum or require_full and len(pairs)!=len(self.matches): return None
        a,b=self.cell(first,0,[p[0] for p in pairs]),self.cell(second,0,[p[1] for p in pairs])
        denominator=abs(a['_number'])+abs(b['_number'])
        return {'score':(a['_number']-b['_number'])/denominator if denominator else 0,
                'weight':weight,'cells':[a,b],'comparison':f'{first} compared with {second} in the same matches',
                'coverage':len(pairs)/len(self.matches)}


def _game_management(profile):
    states={row['key']:row for row in (profile or {}).get('states') or []}
    lead,behind=states.get('ahead',{}),states.get('behind',{})
    comparisons=[]
    configs=[(lead,'Lead Win Rate (%)','Lead Dropped Points Rate (%)',lead.get('finalWins'),
              (lead.get('finalDraws',0)+lead.get('finalLosses',0)) if 'finalDraws' in lead and 'finalLosses' in lead else None),
             (behind,'Trailing Equalizer Rate (%)','Trailing No Equalizer Rate (%)',behind.get('matchesToLevel'),
              behind.get('matchesEntered',0)-behind['matchesToLevel'] if 'matchesToLevel' in behind else None)]
    for state,first,second,a,b in configs:
        count=state.get('matchesEntered',0)
        if count<3 or a is None or b is None or not 0<=a<=count or not 0<=b<=count: continue
        cells=[]
        for name,value in [(first,a),(second,b)]:
            cells.append({'label':name,'value':f'%{value/count*100:.2f}'.rstrip('0').rstrip('.'),
                          'contributes':True,'basis':'rate','matchesCovered':count,'sampleSize':count,
                          'detail':f'{value} / {count}', '_number':value/count*100})
        comparisons.append({'score':(a-b)/count,'weight':1,'cells':cells,
                            'comparison':'conditional outcomes in distinct matches entering this state','coverage':1})
    return comparisons if len(comparisons)==2 else []


def _candidates_from_evidence(evidence,score_flow=None):
    if not evidence.matches: return []
    pair=evidence.paired
    # Two comparisons per scope produce a coherent, multi-metric four-cell finding.
    configs={
      'creation':[pair('Key Passes'),pair('Big Chances Created')],
      'finishing':[pair('On Target Goal Conversion (%)') or evidence.internal('Goals','Expected Goals',require_full=True),pair('Goal Conversion (%)')],
      'passing':[pair('Accurate Passes (%)',weight=2),pair('Passes',weight=.35)],
      'progression':[pair('Final-third Pass Share (%)') or pair('Outfield Final-third Pass Share (%)'),pair('Passes In Final Third') or pair('Outfield Passes In Final Third')],
      'dribbling':[pair('Dribble Accuracy (%)',weight=2),pair('Successful Dribbles',weight=.35)],
      'set_pieces':[pair('Expected Goals Set Play (xGSP)',weight=2),pair('Corners',weight=.35)],
      'duels':[pair('Duels Won (%)',weight=2) or pair('Outfield Duels Won (%)',weight=2),pair('Aerials Won (%)') or pair('Duels Won',weight=.35) or pair('Outfield Duels Won',weight=.35)],
      'recoveries':[pair('Recoveries Per 100 Opponent Passes') or pair('Outfield Recoveries Per 100 Opponent Passes'),pair('Interceptions Per 100 Opponent Passes') or pair('Outfield Interceptions Per 100 Opponent Passes')],
      'threat':[pair('Shot Quality (%)') or pair('Inside-box Shot Share (%)'),pair('Shots On Target (%)')],
      'errors':[pair('Error Lead To Shot',inverse=True),pair('Error Lead To Goal',inverse=True)],
      'discipline':[pair('Fouls Per 100 Opponent Passes',inverse=True),pair('Total Cards',inverse=True) or pair('Yellow Cards',inverse=True)],
      'game_management':_game_management(score_flow),
    }
    result=[]
    for identity,comparisons in configs.items():
        if len(comparisons)!=2 or any(c is None for c in comparisons): continue
        weight=sum(c['weight'] for c in comparisons)
        score=sum(c['score']*c['weight'] for c in comparisons)/weight
        if abs(score)<.025: continue  # A near tie does not establish a finding.
        side='strength' if score>0 else 'weakness'
        # Efficiency-led topics require the efficiency signal to agree with the conclusion.
        if identity in {'finishing','passing','dribbling','duels','set_pieces'} and comparisons[0]['score']*score<=0: continue
        supporting=sum(c['weight'] for c in comparisons if c['score']*score>0)/weight
        if supporting<.6: continue
        coverage=min(c['coverage'] for c in comparisons)
        cells={cell['label']:{k:v for k,v in cell.items() if k!='_number'} for c in comparisons for cell in c['cells']}
        if len(cells)<3: continue
        result.append({'findingId':identity,'findingSide':side,'score':round(abs(score)*coverage,5),
                       'source_metrics':cells,'comparisons':comparisons,'sampleSize':len(evidence.matches)})
    return sorted(result,key=lambda row:(-row['score'],row['findingId']))


def candidates(reports,team_id,score_flow=None):
    evidence=Evidence(reports,team_id)
    result=_candidates_from_evidence(evidence,score_flow)
    for row in result:
        row.update(findingScope='selected_matches',supportingMatches=len(evidence.matches))
    # Missing-data coverage is allowed and disclosed per cell. Outcome-based
    # subsets are not: every candidate is evaluated against the selected sample.
    return result


def select_findings(reports,team_id,side,score_flow=None,confirmed=None):
    if side not in {'strength','weakness'}: raise ValueError('Unknown finding side')
    chosen=[]
    confirmed=(confirmed or {}).get('themes') or []
    excluded={row.get('findingId') for row in confirmed}
    for item in candidates(reports,team_id,score_flow):
        if item['findingSide']!=side or item['findingId'] in excluded: continue
        labels=set(item['source_metrics'])
        others=[*chosen,*confirmed]
        if any(len(labels & set(row.get('source_metrics') or {m['label']:m for m in row.get('metrics') or []}))/max(1,len(labels))>.65 for row in others): continue
        chosen.append(item)
        if len(chosen)==3: break
    return chosen


def bind_findings(draft,selected,lang):
    result=[]
    for candidate in selected:
        identity,side=candidate['findingId'],candidate['findingSide']
        definition=CATALOG[identity]
        row=next((row for row in draft.get('themes') or [] if row.get('findingId')==identity),None)
        if row is None: raise ValueError('Missing selected performance finding')
        bound=bind_row_evidence({**row,'findingId':identity,'findingSide':side,
            'findingScope':candidate.get('findingScope','selected_matches'),
            'supportingMatches':candidate.get('supportingMatches',candidate['sampleSize']),
            'sampleSize':candidate['sampleSize'],
            'title':definition[side][0 if lang=='tr' else 1],
            'description':definition['description'][0 if lang=='tr' else 1]},candidate['source_metrics'],4)
        if any(len(refs)<2 for refs in bound.get('evidence_metrics') or []):
            bound['evidence_validation'].append('Each finding paragraph must connect at least two supporting source metrics.')
        bound['metrics']=list(deepcopy(candidate['source_metrics']).values())
        bound['evidence_values']=deepcopy(candidate['source_metrics'])
        result.append(bound)
    return {'themes':result,'catalogVersion':CATALOG_VERSION}


def _paragraph_shape(draft,selected):
    result=deepcopy(draft)
    for candidate in selected:
        row=next((row for row in result.get('themes') or [] if row.get('findingId')==candidate['findingId']),None)
        if row is None: raise ValueError('Missing selected finding')
        value=row.get('analysis')
        values=[value] if isinstance(value,str) else value if isinstance(value,list) else []
        paragraphs=[p.strip() for text in values if isinstance(text,str)
                    for p in re.split(r'\n\s*\n',text) if p.strip()]
        if len(paragraphs)!=2: raise ValueError('A finding needs exactly two analysis paragraphs')
        row['analysis']=paragraphs
    return result


def _write_findings(selected,team_name,side,lang,confirmed=None):
    if not selected:
        return {'themes':[],'catalogVersion':CATALOG_VERSION,'narrativeVersion':NARRATIVE_VERSION}
    if not os.getenv('OPENAI_API_KEY'): raise RuntimeError('Report generation is not configured')
    selected=[{**item,'title':CATALOG[item['findingId']][side][0 if lang=='tr' else 1],
               'description':CATALOG[item['findingId']]['description'][0 if lang=='tr' else 1],
               'interpretation_focus':INTERPRETATION_FOCUS[item['findingId']]} for item in selected]
    messages=[
        ('system', "You are ScoutWise Enterprise's senior football performance analyst. Return JSON with themes only. Return exactly the supplied selected findings (normally THREE) in their supplied order. Copy findingId exactly; never rename, replace or add topics. Each theme has findingId, analysis and evidence_metrics. analysis MUST be a JSON array of exactly TWO strings, not a single string. Each string is ONE paragraph of 30-38 words with no embedded newline or bullet. evidence_metrics MUST be exactly TWO arrays, aligned with the two analysis strings. State the team's specific football advantage or failure mechanism first, then connect the supporting relationship to its practical value or cost. The second paragraph must add a different implication, not paraphrase the first. Use at most ONE short numerical fact per paragraph; the visible cells already show the numbers. Each paragraph's evidence_metrics contains at least TWO related exact names from THIS finding's source_metrics. Every inference must actually follow from the cited relationship, not merely mention two metrics. Discuss relationships between multiple metrics; never define or recite a single metric. All visible diagnostic cells are fixed by code. Compare only the supplied paired observations or opportunity/outcome relationships. Own and opponent observations refer to the same covered matches. All findings evaluate the full selected sample. matchesCovered records source-data availability only, never favorable-match selection. Never present a partial-coverage value as the overall selected-match average. Do not imply a contradiction with a full-sample strength in another distinct topic. No external benchmarks, invented tactics, causal claims, pitch locations, player attributes or recommendations. Missing data is unknown, not zero. Covered standard-event zeros can support a lower-risk strength only through a supplied nonzero comparison; zero adverse events cannot be weaknesses. Percentages are pooled where count inputs exist. Counts are per match; per-100-opponent-pass activity and conditional game-state rates retain their stated bases. Goal output versus xG is a finishing comparison, not a defensive measure. Set-play xG includes all recorded set pieces; corners are only one opportunity type. Never divide total set-play xG by corners or claim all set-play xG came from corners. Shot quality is xG per shot; do not confuse it with possession or finishing. Game-state rates count each match once and are conditional on entering that state; winning after leading does not prove the lead was never lost. Avoid claiming recurring tendencies from very few observations. Strength prose is positive and evidence-led; weakness prose diagnoses the demonstrated adverse relationship without praise or softening. Confirmed strengths are binding: never reverse their finding or repackage the same evidence as a weakness. Use natural requested-language football terminology and translate metric references in prose. Never display classifier scores or implementation details. Do not write a higher/lower-value recap, generic supporting-the-finding sentences, table-reading instructions, or a dictionary definition. Reject wording that could be pasted into another team report without changing its football conclusion. Do not use longer prose to manufacture depth. Avoid empty closing phrases such as pratik değerini artırıyor, hücum planının değerini artırıyor, bu ilişki bulguyu gösteriyor. Explain the concrete consequence instead. Use dripling, never çalım or top sürme, for dribbling in Turkish. No markdown." + NARRATIVE_CONTRACT + FOOTBALL_TERMINOLOGY_GUIDANCE + PROFILE_EVIDENCE_GUIDANCE),
        ('human',json.dumps({'language':lang,'team':team_name,'section':side,'selected_findings':selected,
                            'confirmed_strengths':confirmed or {}},ensure_ascii=False))
    ]
    draft=generate_profile_json(messages)
    try:
        draft=_paragraph_shape(draft,selected)
    except ValueError:
        draft=generate_profile_json([*messages,('assistant',json.dumps(draft,ensure_ascii=False)),
            ('human','Correct only the analysis structure. For every supplied findingId, return analysis as exactly TWO single-paragraph strings of 30-38 words and evidence_metrics as TWO aligned arrays. Preserve the supported findings and source evidence. Never combine the paragraphs in one string or add a third paragraph.')])
        draft=_paragraph_shape(draft,selected)
    result=bind_findings(draft,selected,lang)
    result['narrativeVersion']=NARRATIVE_VERSION
    return result



def _team_name(reports,team_id,lang):
    return next((team['name'] for report in reports for team in report.get('teams') or []
                 if int(team.get('id') or 0)==int(team_id) and team.get('name')),
                'Takım' if lang=='tr' else 'The team')


def build_findings(reports,team_id,side,lang='en',score_flow=None,confirmed=None):
    if score_flow is None:
        from match_report_module.report import build_team_report_score_flow_profile
        score_flow=build_team_report_score_flow_profile(reports,team_id,lang,include_ai=False)
    selected=select_findings(reports,team_id,side,score_flow,confirmed)
    return _write_findings(selected,_team_name(reports,team_id,lang),side,lang,confirmed)


def _archived_selection(profile,side):
    selected=[]
    for theme in profile.get('themes') or []:
        if theme.get('findingScope') == 'supporting_matches': return None
        identity=theme.get('findingId')
        if identity not in CATALOG: return None
        source={cell['label']:deepcopy(cell) for cell in theme.get('metrics') or []}
        if len(source)<2: return None
        selected.append({'findingId':identity,'findingSide':side,
                         'findingScope':theme.get('findingScope','selected_matches'),
                         'supportingMatches':theme.get('supportingMatches',theme.get('sampleSize',0)),
                         'sampleSize':theme.get('sampleSize',0),'source_metrics':source})
    return selected


def upgrade_findings(content,team_id,lang):
    def current(field):
        profile=content.get(field) or {}
        return (profile.get('catalogVersion')==CATALOG_VERSION and profile.get('narrativeVersion')==NARRATIVE_VERSION
                and all(t.get('findingScope')!='supporting_matches' for t in profile.get('themes') or []))
    strength_current,weakness_current=current('strengths'),current('weaknesses')
    if strength_current and weakness_current or not content.get('reports'): return content
    reports=content['reports'];flow=content.get('scoreFlowProfile')
    if flow is None:
        from match_report_module.report import build_team_report_score_flow_profile
        flow=build_team_report_score_flow_profile(reports,team_id,lang,include_ai=False)
    result=dict(content);name=_team_name(reports,team_id,lang)
    try:
        if not strength_current:
            existing=content.get('strengths') or {}
            selected=_archived_selection(existing,'strength') if existing.get('catalogVersion')==CATALOG_VERSION else None
            if selected is None: selected=select_findings(reports,team_id,'strength',flow)
            result['strengths']=_write_findings(selected,name,'strength',lang)
        if not weakness_current:
            existing=content.get('weaknesses') or {}
            selected=_archived_selection(existing,'weakness') if existing.get('catalogVersion')==CATALOG_VERSION else None
            if selected is None: selected=select_findings(reports,team_id,'weakness',flow,result.get('strengths'))
            result['weaknesses']=_write_findings(selected,name,'weakness',lang,result.get('strengths'))
        from report_module.profile_editor import review_team_profiles
        pending={field for field,is_current in [('strengths',strength_current),('weaknesses',weakness_current)] if not is_current}
        reviewed=review_team_profiles({field:result.get(field) or {} for field in ('attackProfile','defenseProfile','strengths','weaknesses')},
                                      lang,editable_sections=pending,task_contract=NARRATIVE_CONTRACT)
        for field in pending: result[field]=reviewed[field]
    except Exception as exc:
        # Preserve a readable saved report and retry the prose upgrade on a later open.
        print(f'[enterprise_team_report] event=finding_narrative_upgrade_deferred error_type={type(exc).__name__}')
        return content
    if content.get('overview'):
        rows=[]
        for row in content['overview']:
            key=row.get('key')
            if key in {'strengths','weaknesses'} and result.get(key) is not content.get(key):
                themes=result[key]['themes']
                rows.append({**row,'summary':(' · '.join(t['title'] for t in themes)),
                    'subBullets':[{'label':t['title'],'text':t['analysis'][0]} for t in themes]})
            else: rows.append(row)
        result['overview']=rows
    return result
