"""Selected-period tactical identity. Team formulas are shared with pre-match reports."""
import hashlib
import json
import os
import re
from collections import defaultdict

from fastapi import Depends, HTTPException, Path
from pydantic import BaseModel, Field
from .progressive import NARRATIVE_LOCKS, progressive_response
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from match_analysis_module.match_analysis import SportMonksError
from standings_module.metrics import player_metric_values, numeric
from . import analysis as history
from . import comparison as comparison

VERSION = 5
PERSPECTIVE_VERSION = 3
ROLE_METRICS = ['Passes', 'Key Passes', 'Shots Total', 'Tackles', 'Interceptions', 'Successful Dribbles', 'Total Crosses', 'Ball Recovery']
DETAIL_METRICS = ROLE_METRICS + ['Clearances', 'Total Duels', 'Duels Won', 'Aerials Won', 'Fouls', 'Passes In Final Third', 'Through Balls', 'Touches', 'Possession Lost', 'Dispossessed', 'Dribble Attempts', 'Tackles Won']


class TacticsRequest(BaseModel):
    periodIds: list[str] = Field(min_length=1, max_length=1000)
    stream: bool = False


def resolve_periods(data, ids):
    wanted = set(ids)
    selected = [p for p in data['periods'] if p['id'] in wanted and p['eligibleMatchCount'] > 0]
    if {p['id'] for p in selected} != wanted:
        raise HTTPException(422, 'Choose available eligible periods.')
    groups = {}
    for p in selected:
        key = f"{p['teamId']}:{p.get('season') or p.get('seasonId') or 'unknown'}"
        groups.setdefault(key, []).append(p)
    if len(groups) > 5:
        raise HTTPException(422, 'Choose up to five team-seasons.')
    return groups


def compact_details(fixture):
    players = []
    seen = set()
    for row in fixture.get('lineups') or []:
        tid, pid = row.get('team_id') or row.get('participant_id'), row.get('player_id')
        details = {(d.get('type') or {}).get('name'): numeric((d.get('data') or {}).get('value')) for d in row.get('details') or []}
        minutes = details.get('Minutes Played')
        if not tid or not pid or minutes is None or minutes <= 0 or (tid, pid) in seen:
            continue
        seen.add((tid, pid))
        position = row.get('position') or {}
        player = row.get('player') or {}
        players.append({'teamId': int(tid), 'id': int(pid), 'name': player.get('display_name') or player.get('name') or '',
            'position': position.get('name') or '', 'positionId': row.get('position_id') or position.get('id'),
            'minutes': minutes, 'metrics': {k: v for k, v in player_metric_values(row).items() if k in DETAIL_METRICS}})
    return {'id': int(fixture['id']), 'formations': [{'teamId': int(r['participant_id']), 'formation': r.get('formation')}
        for r in fixture.get('formations') or [] if r.get('participant_id') and r.get('formation')], 'players': players}


def load_details(db, ids):
    # Ten fixtures per request bounds lineup-detail payloads; retain only role inputs.
    for offset in range(0, len(ids), 10):
        batch = ids[offset:offset + 10]
        keys = [f'tactics-fixture:3:{fid}' for fid in batch]
        rows = db.execute(text('SELECT payload FROM enterprise_coach_comparison_cache WHERE cache_key=ANY(CAST(:keys AS text[])) AND expires_at>now()'), {'keys': keys}).scalars().all()
        values = {int(row['id']): row for row in rows}
        missing = [fid for fid in batch if fid not in values]
        if missing:
            with history._request_slots:
                try:
                    response = history._get('fixtures/multi/' + ','.join(map(str, missing)), {
                        'include': 'formations;lineups.player;lineups.position;lineups.details.type'})
                except SportMonksError as exc:
                    if 'HTTP 403' not in str(exc): raise
                    try:
                        response = history._get('fixtures/multi/' + ','.join(map(str, missing)), {'include': 'formations'})
                    except SportMonksError as limited:
                        if 'HTTP 403' not in str(limited): raise
                        response = {'data': [{'id': fid} for fid in missing]}
            fresh = {int(f['id']): compact_details(f) for f in response.get('data') or [] if int(f['id']) in missing}
            del response
            if set(fresh) != set(missing):
                raise SportMonksError('Tactical match details are temporarily incomplete. Please try again.')
            entries = [{'key': f'tactics-fixture:3:{fid}', 'payload': value} for fid, value in fresh.items()]
            db.execute(text('''INSERT INTO enterprise_coach_comparison_cache(cache_key,payload,expires_at)
                SELECT item->>'key',item->'payload',now()+interval '24 hours' FROM jsonb_array_elements(CAST(:items AS jsonb)) AS item
                ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at,generated_at=now()'''), {'items': json.dumps(entries)})
            db.commit()
            values.update(fresh)
        yield from (values[fid] for fid in batch)


def role_name(player):
    name = player['position'].casefold()
    if player.get('positionId') == 24 or 'goalkeeper' in name: return 'goalkeeper'
    if player.get('positionId') == 25 or 'defender' in name: return 'defender'
    if player.get('positionId') == 26 or 'midfielder' in name: return 'midfielder'
    if player.get('positionId') == 27 or any(word in name for word in ('attacker', 'forward', 'striker')): return 'attacker'
    return 'unknown'


def summarize_details(rows, team_id, observe=None):
    formations, roles = defaultdict(int), {}
    coverage, position_coverage, lineup_coverage = defaultdict(set), defaultdict(lambda: defaultdict(set)), 0
    totals = defaultdict(float)
    for fixture in rows:
        if observe is not None: observe(fixture)
        formation = next((r['formation'] for r in fixture['formations'] if r['teamId'] == team_id), None)
        if formation: formations[formation] += 1
        players = [p for p in fixture['players'] if p['teamId'] == team_id]
        if players: lineup_coverage += 1
        for player in players:
            role = role_name(player)
            target = roles.setdefault(role, {'role': role, 'minutes': 0, 'playerIds': set(), 'metrics': defaultdict(float)})
            target['minutes'] += player['minutes']; target['playerIds'].add(player['id'])
            for metric, value in player['metrics'].items():
                if value < 0: continue
                target['metrics'][metric] += value; totals[metric] += value
                coverage[metric].add(fixture['id']); position_coverage[role][metric].add(fixture['id'])
    return {'formations': [{'formation': name, 'matches': count} for name, count in sorted(formations.items(), key=lambda item: (-item[1], item[0]))],
        'formationCoverage': sum(formations.values()), 'lineupCoverage': lineup_coverage,
        'roleMetricCoverage': {name: len(ids) for name, ids in coverage.items()},
        'roles': [{'role': r['role'], 'minutes': round(r['minutes']), 'players': len(r['playerIds']),
            'metrics': {name: {'total': round(value, 2), 'share': round(value / totals[name] * 100, 2) if totals[name] else None,
                'matchesCovered': len(position_coverage[r['role']][name])} for name, value in r['metrics'].items()}}
            for r in roles.values()]}


def paired_long_ball_share(fixtures, team_id):
    numerator, denominator, covered = 0., 0., 0
    for fixture in fixtures:
        values = {(r.get('type') or {}).get('name'): numeric((r.get('data') or {}).get('value'))
            for r in fixture.get('statistics') or [] if int(r.get('participant_id') or 0) == team_id}
        long_balls, passes = values.get('Long Passes', values.get('Long Balls')), values.get('Passes')
        if long_balls is not None and passes is not None and passes > 0 and 0 <= long_balls <= passes:
            numerator += long_balls; denominator += passes; covered += 1
    return {'name': 'Long Ball Share (%)', 'value': round(numerator / denominator * 100, 2), 'matches_covered': covered, 'aggregation': 'derived_rate'} if denominator else None


def balance_matches(fixtures, team_id):
    rows = []
    for fixture in fixtures:
        participants = [int(p['id']) for p in fixture.get('participants') or [] if p.get('id')]
        other = next((pid for pid in participants if pid != team_id), None)
        if other is None: continue
        values = {}
        for row in [*(fixture.get('statistics') or []), *(fixture.get('xgfixture') or fixture.get('xGFixture') or [])]:
            name = (row.get('type') or {}).get('name')
            value = numeric((row.get('data') or {}).get('value'))
            if name in {'Shots Total', 'Expected Goals', 'Expected Goals (xG)'} and value is not None and value >= 0:
                values.setdefault((int(row.get('participant_id') or 0), 'xg' if name != 'Shots Total' else 'shots'), value)
        rows.append({'fixtureId': fixture['id'], 'date': str(fixture.get('starting_at') or '')[:10],
            'shotsFor': values.get((team_id, 'shots')), 'shotsAgainst': values.get((other, 'shots')),
            'xgFor': values.get((team_id, 'xg')), 'xgAgainst': values.get((other, 'xg'))})
    return rows


def shorten_perspective(value, limit=78):
    words = value.split()
    if len(words) <= limit: return value
    # End at a sentence boundary; never cut a numerical statement mid-sentence.
    import re
    sentences = re.split(r'(?<=[.!?])\s+', value.strip())
    selected = []
    for sentence in sentences:
        if selected and len((' '.join(selected) + ' ' + sentence).split()) > limit: break
        selected.append(sentence)
    return ' '.join(selected)


def perspective_evidence(periods):
    sections = {'playing_character': [], 'formations': [], 'balance': [], 'playing_patterns': [], 'player_contributions': []}
    aliases = {'Expected Goals (xG)': 'Expected Goals', 'Expected Goals on Target (xGoT)': 'Expected Goals On Target'}
    character = {'Ball Possession %', 'Accurate Passes (%)', 'Long Ball Share (%)', 'Shot Share (%)', 'Goal Conversion (%)', 'Shots On Target (%)', 'Dribble Accuracy (%)'}
    patterns = {'Passes', 'Key Passes', 'Passes In Final Third', 'Accurate Crosses (%)', 'Long Balls Won (%)', 'Dangerous Attacks', 'Big Chances Created', 'Shots Insidebox', 'Shots Outsidebox', 'Total Crosses', 'Corners', 'Opponent Dangerous Attacks', 'Opponent Big Chances Created', 'Interceptions', 'Tackles', 'Blocked Shots', 'Saves', 'Duels Won'}
    for p in periods:
        # Sparse inputs are omitted from the synthesis rather than described as warnings.
        metrics = {aliases.get(m['name'], m['name']): m for rows in p['categories'].values() for m in rows
            if numeric(m.get('value')) is not None and m['value'] != 0 and (m.get('matches_covered') or 0) >= max(5, p['matchCount'] * .8)}
        identity = {'team': p['team']}
        for section, names in [('playing_character', character), ('playing_patterns', patterns)]:
            available = {name: m['value'] for name, m in metrics.items() if name in names}
            if available: sections[section].append({**identity, 'metrics': available})
        if p['formations']:
            sections['formations'].append({**identity, 'choices': p['formations'][:3]})
        if 'Shots Total' in metrics and 'Opponent Shots Total' in metrics:
            sections['balance'].append({**identity, 'shotsFor': metrics['Shots Total']['value'], 'shotsAgainst': metrics['Opponent Shots Total']['value']})
        roles = []
        for role in p['roles']:
            values = {name: m['share'] for name, m in role['metrics'].items() if m['share'] is not None and m['share'] > 0
                and p['roleMetricCoverage'].get(name, 0) >= max(5, p['matchCount'] * .8)}
            if values: roles.append({'role': role['role'], 'contributionShares': values})
        if roles: sections['player_contributions'].append({**identity, 'roles': roles})
    return {name: rows for name, rows in sections.items() if rows}


def fallback_profile(evidence):
    result = {'tr': {}, 'en': {}}
    def add(section, tr, en):
        if section in evidence:
            result['tr'][section] = tr; result['en'][section] = en
    character = evidence.get('playing_character', [])
    possession = [r['metrics']['Ball Possession %'] for r in character if 'Ball Possession %' in r['metrics']]
    if possession and sum(possession)/len(possession) >= 50:
        add('playing_character', 'Topa sahip olarak oyunu yönlendiren, pas bağlantılarıyla hücum sürekliliği arayan bir oyun karakteri öne çıkıyor.', 'The playing identity favours possession, connected passing and sustained attacking involvement.')
    else:
        add('playing_character', 'Topla oyunu pas bağlantıları üzerinden kuran, hücum etkinliğini şut üretimiyle birleştiren bir profil öne çıkıyor.', 'The playing identity connects passing patterns with attacking involvement and shot production.')
    choices = defaultdict(int)
    for row in evidence.get('formations', []):
        for item in row['choices']: choices[item['formation']] += item['matches']
    if choices:
        ordered = sorted(choices,key=choices.get,reverse=True)
        if len(ordered)>1:
            add('formations', f'{ordered[0]} ana yerleşim olarak öne çıkarken {ordered[1]} farklı takım dönemlerinde kullanılan bir alternatif oluşturuyor.', f'{ordered[0]} is the main recorded shape, with {ordered[1]} providing an alternative across team periods.')
        else:
            add('formations', f'{ordered[0]} yerleşimindeki süreklilik, tercih edilen takım yapısının belirgin bir parçasını oluşturuyor.', f'Continuity in {ordered[0]} is a defining feature of the recorded team structure.')
    balance = evidence.get('balance', [])
    stronger = sum(r['shotsFor']-r['shotsAgainst'] for r in balance)>0
    add('balance', 'Rakibinden daha fazla şut üreten yapı, hücum ağırlığı taşıyan bir oyun dengesi ortaya koyuyor.' if stronger else 'Hücum üretimi ile rakibe bırakılan şutlar, oyunun savunma yükünü de taşıyan bir denge ortaya koyuyor.', 'The balance of shots favours attacking output over the production conceded to opponents.' if stronger else 'Shot production and the chances allowed describe a balance that also carries a defensive workload.')
    available={name for row in evidence.get('playing_patterns',[]) for name in row['metrics']}
    routes=[(name,tr,en) for name,tr,en in [('Key Passes','kilit paslar','key passes'),('Shots Insidebox','ceza sahası içinden şutlar','shots inside the box'),('Total Crosses','kanat ortaları','crosses from wide areas'),('Passes','pas bağlantıları','passing connections'),('Interceptions','araya girmeler','interceptions'),('Tackles','müdahaleler','tackles')] if name in available][:2]
    if routes:
        tr_routes=' ve '.join(r[1] for r in routes)
        en_routes=' and '.join(r[2] for r in routes)
        add('playing_patterns', f'Oyun alışkanlıklarında {tr_routes}, takımın sahadaki çözüm yollarını belirginleştiriyor.', f'The recorded playing patterns feature {en_routes} as routes through the game.')

    # Describe the observed leading role rather than inventing off-ball instructions.
    roles = defaultdict(float)
    for row in evidence.get('player_contributions', []):
        for role in row['roles']:
            roles[role['role']] += role['contributionShares'].get('Key Passes',0)
    leading = max(roles,key=roles.get) if roles and max(roles.values())>0 else None
    role_labels = {'midfielder': ('Orta saha', 'Midfield'), 'defender': ('Savunma hattı', 'The defensive unit'), 'attacker': ('Hücum hattı', 'The attacking unit')}
    if leading in role_labels:
        tr_role,en_role=role_labels[leading]
        add('player_contributions', f'{tr_role}, kilit pas katkısıyla pozisyon hazırlığının merkezinde yer alıyor.', f'{en_role} leads the recorded key-pass contribution and plays a central part in chance creation.')
    else:
        add('player_contributions', 'Pas, şut ve top kazanma katkılarının mevkilere dağılımı, hücum ve savunma iş bölümünü ortaya koyuyor.', 'The positional distribution of passing, shooting and ball recovery describes the recorded division of attacking and defensive contributions.')
    return result


def valid_profile_sentence(sentence, formations):
    if not isinstance(sentence,str) or not sentence.strip(): return False
    value=sentence.strip()
    if len(value.split())>30: return False
    used=re.findall(r'(?<![\w-])\d(?:-\d){2,4}(?![\w-])',value)
    if any(shape not in formations for shape in used): return False
    without_shapes=re.sub(r'(?<![\w-])\d(?:-\d){2,4}(?![\w-])','',value)
    if re.search(r'\d|%|yüzde|percent|sınırlı|atıf|atfed|dikkat gerektir|yetersiz|limited|caution|attribution|insufficient|iç koridor|half.space|dönem ilerledikçe|geniş oyun|pozitif seyrediyor|rakibe verilen şut',without_shapes,re.I): return False
    if re.search(r'(?:dört|üç|iki|bir|beş|four|three|two|one|five)(?:[- ](?:dört|üç|iki|bir|beş|four|three|two|one|five)){2,}',value,re.I): return False
    return len(re.split(r'(?<=[.!?])\s+',value))==1


def perspective(periods):
    evidence=perspective_evidence(periods)
    fallback=fallback_profile(evidence)
    if not os.getenv('OPENAI_API_KEY'):
        return {lang:' '.join(fallback[lang].values()) for lang in ('tr','en')}
    try:
        from langchain_openai import ChatOpenAI
        response=ChatOpenAI(model=os.getenv('OPENAI_MATCH_REPORT_MODEL',os.getenv('OPENAI_REPORT_MODEL','gpt-5.6-luna')),
            api_key=os.environ['OPENAI_API_KEY'],temperature=.2,timeout=35,max_retries=0).invoke([
            ('system', 'Return JSON with exactly tr and en. Each contains an object keyed exactly by the supplied evidence sections, in that order. Write exactly ONE concise sentence per section, revealing the coach playing profile across the selected periods: playing_character, formations, balance, playing_patterns, player_contributions. Use fluent, natural Turkish football terminology and idiomatic English, not translated statistical prose. Prefer pas dolaşımı, kanat oyunu, ceza sahası içinden şutlar, top kazanımı, pozisyon hazırlığı and hücum-savunma dengesi when those specific inputs support them. Never use awkward literal translations such as geniş oyun, üretim pozitif seyrediyor or rakibe verilen şutlar. Shots Insidebox means ceza sahası içinden şutlar; it does NOT establish iç koridor or half-space attacks. There is no chronological evidence: describe differences between periods without claiming a progression over time or saying dönem ilerledikçe. Aim for 60-85 words TOTAL per language. Synthesize recurring tendencies and meaningful adaptations; do not list clubs or seasons, describe the page, repeat the same tendency across sections or give instructions. NO numerical statistics, percentages, counts, dates, rankings or written-out quantities. The ONLY allowed numerical expressions are recorded formation strings, written exactly as 4-2-3-1, never dört-iki-üç-bir or four-two-three-one. Do not discuss limitations, attribution, sparse data, caution, competition differences or missing metrics; sparse inputs were already removed. Never say sınırlıdır, sınırlı, dikkat gerektirir, atıf, örneklem. Player-contribution evidence MUST inform its own sentence: identify the role groups driving creativity, ball carrying, wide play or ball recovery. Use only supplied evidence; never invent pressing intensity, defensive line height, transitions, off-ball roles or coach causation. Omit unsupported details rather than qualifying them. Each sentence must contain a substantive football observation from its own section.'),
            ('human',json.dumps(evidence))])
        raw=str(response.content).strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        value=json.loads(raw)
        formations={r['formation'] for p in periods for r in p['formations']}
        for lang in ('tr','en'):
            for section in evidence:
                sentence=(value.get(lang) or {}).get(section)
                if valid_profile_sentence(sentence,formations): fallback[lang][section]=sentence.strip()
    except Exception:
        pass
    return {lang:' '.join(fallback[lang].values()) for lang in ('tr','en')}


def build_tactics(db, coach_id, data, ids, include_ai=True):
    groups = resolve_periods(data, ids)
    signature = [[key, sorted({fid for p in rows for fid in p['fixtureIds']})] for key, rows in sorted(groups.items())]
    key = 'tactics:' + hashlib.sha256(json.dumps([VERSION, coach_id, signature]).encode()).hexdigest()
    if not include_ai:
        key += ':data:1'
    cached = comparison.read_cache(db, key)
    if cached is not None and (not include_ai or cached.get('perspectiveVersion') == PERSPECTIVE_VERSION):
        return cached
    # Resolve data before taking the AI cache lock: data requests never wait for generation.
    base = None if cached is not None or not include_ai else build_tactics(db, coach_id, data, ids, include_ai=False)
    locks = NARRATIVE_LOCKS if include_ai else comparison._locks
    with locks[int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(locks)]:
        cached = comparison.read_cache(db, key)
        if cached is not None:
            if include_ai and cached.get('perspectiveVersion') != PERSPECTIVE_VERSION:
                if db is not None:
                    db.rollback()  # Return the read transaction's connection during the model call.
                cached = {**cached, 'perspective': perspective(cached['periods']), 'perspectiveVersion': PERSPECTIVE_VERSION}
                comparison.write_cache(db, key, cached, 6)
            return cached
        if base is None:
            result = []
            for scope_id, periods in groups.items():
                first = periods[0]; team_id = first['teamId']; fixture_ids = sorted({fid for p in periods for fid in p['fixtureIds']})
                from .evidence import load_period
                shared = load_period(db, coach_id, team_id, fixture_ids)
                result.append({'id': scope_id, 'teamId': team_id, 'team': first['team'], 'teamImageUrl': first['teamImageUrl'], 'season': first['season'],
                    'competitions': sorted({p['competition'] for p in periods}), 'matchCount': len(fixture_ids), **shared['tactics']})
        else:
            result = base['periods']
        if include_ai and db is not None:
            db.rollback()
        value = {'periods': result, 'perspective': perspective(result) if include_ai else {'tr': '', 'en': ''}}
        if include_ai:
            value['perspectiveVersion'] = PERSPECTIVE_VERSION
        comparison.write_cache(db, key, value, 6)
        return value


@history.router.post('/coach-analysis/{coach_id}/tactics')
def coach_tactics(body: TacticsRequest, coach_id: int = Path(gt=0), user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        data = history.get_periods(db, coach_id)
        if body.stream:
            first = build_tactics(db, coach_id, data, body.periodIds, include_ai=False)
            return progressive_response(first, lambda: build_tactics(db, coach_id, data, body.periodIds))
        return build_tactics(db, coach_id, data, body.periodIds)
    except SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None
