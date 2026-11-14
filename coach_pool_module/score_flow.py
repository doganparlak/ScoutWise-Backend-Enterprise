"""Validated, coach-oriented score timelines using the team-report state model."""
import hashlib
import json
import re
from threading import Lock

from fastapi import Depends, HTTPException, Path
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from match_analysis_module.pre_match_report import _aggregate_score_flow
from . import analysis as history, comparison
from .momentum import MomentumRequest as ScoreFlowRequest, number, resolve_periods

VERSION = 2
SOURCE_VERSION = 1
BATCH_SIZE = 10
_locks = [Lock() for _ in range(16)]


def timeline(fixture):
    participants = {int(p['id']): (p.get('meta') or {}).get('location') for p in fixture.get('participants') or [] if p.get('id')}
    if len(participants) != 2 or set(participants.values()) != {'home', 'away'}:
        return None
    home = next(tid for tid, side in participants.items() if side == 'home')
    away = next(tid for tid, side in participants.items() if side == 'away')
    final = history.fixture_scores(fixture)
    if set(final) != set(participants):
        return None
    state = fixture.get('state') or {}
    if (state.get('short_name') or state.get('state')) not in {'FT', 'AET', 'FT_PEN', 'PEN'}:
        return None
    periods = {p.get('id'): p for p in fixture.get('periods') or []}
    shootouts = {pid for pid, p in periods.items() if any(word in (
        str(p.get('description') or '') + str((p.get('type') or {}).get('code') or '')).lower()
        for word in ('shootout', 'shoot-out', 'penalt'))}
    # Match the team report's nominal-duration convention, including recorded ET.
    duration = max(90., number(fixture.get('length')) or 90.)
    for pid, period in periods.items():
        start, length = number(period.get('counts_from')), number(period.get('period_length'))
        if pid not in shootouts and start is not None and length is not None and 0 <= start <= 120 and 0 < length <= 90:
            duration = max(duration, start + length)
    if (state.get('short_name') or state.get('state')) == 'AET' and duration <= 90:
        return None  # Extra-time duration cannot be inferred from the score alone.
    if duration > 180:
        return None
    goals, seen = [], set()
    for index, event in enumerate(fixture.get('events') or []):
        if event.get('rescinded') or event.get('period_id') in shootouts:
            continue
        kind = re.sub(r'[^a-z]', '', str((event.get('type') or {}).get('code') or (event.get('type') or {}).get('name') or '').lower())
        if kind not in {'goal', 'owngoal', 'penalty', 'penaltygoal'}:
            continue
        if event.get('id') is not None:
            if event['id'] in seen:
                continue
            seen.add(event['id'])
        goals.append((index, event, kind))
    if all(number(event.get('sort_order')) is not None for _, event, _ in goals):
        goals.sort(key=lambda item: (number(item[1].get('sort_order')), item[0]))
    else:
        goals.sort(key=lambda item: (
            number((periods.get(item[1].get('period_id')) or {}).get('counts_from'))
            if number((periods.get(item[1].get('period_id')) or {}).get('counts_from')) is not None
            else number(item[1].get('minute')) or 0,
            (number(item[1].get('minute')) or 0) + (number(item[1].get('extra_minute')) or 0), item[0]))
    score = (0, 0)
    normalized, cursor = [], 0.
    for _, event, kind in goals:
        parsed = re.fullmatch(r'\s*(\d+)\s*[-:]\s*(\d+)\s*', str(event.get('result') or ''))
        if parsed:
            updated = tuple(map(int, parsed.groups()))
        else:
            tid = event.get('participant_id') or event.get('team_id')
            if kind == 'owngoal' or tid not in participants:
                return None
            updated = (score[0] + int(tid == home), score[1] + int(tid == away))
        delta = (updated[0] - score[0], updated[1] - score[1])
        if delta == (0, 0):
            continue  # Duplicate representation of an already-counted goal.
        if delta not in {(1, 0), (0, 1)}:
            return None  # A score jump cannot supply the missing goal time.
        minute = number(event.get('minute'))
        extra = number(event.get('extra_minute')) if event.get('extra_minute') is not None else 0.
        if minute is None or extra is None or minute < 0 or extra < 0 or minute + extra > 180:
            return None
        cursor = max(cursor, minute + extra)
        normalized.append({'minute': cursor, 'home': updated[0], 'away': updated[1]})
        score = updated
    if score != (final[home], final[away]):
        return None
    return {'duration': max(duration, cursor), 'goals': normalized}


def slim_fixture(fixture):
    return {'id': int(fixture['id']),
            'participants': [{k: p.get(k) for k in ('id', 'meta')} for p in fixture.get('participants') or []],
            'coaches': [{'id': c.get('id'), 'meta': c.get('meta') or {}} for c in fixture.get('coaches') or []],
            'timeline': timeline(fixture)}


def load_fixtures(db, ids):
    for offset in range(0, len(ids), BATCH_SIZE):
        batch = ids[offset:offset + BATCH_SIZE]
        keys = [f'score-flow-fixture:{SOURCE_VERSION}:{fid}' for fid in batch]
        rows = db.execute(text('''SELECT payload FROM enterprise_coach_comparison_cache
            WHERE cache_key=ANY(CAST(:keys AS text[])) AND expires_at>now()'''), {'keys': keys}).scalars().all()
        values = {int(row['id']): row for row in rows}
        missing = [fid for fid in batch if fid not in values]
        if missing:
            with history._request_slots:
                response = history._get('fixtures/multi/' + ','.join(map(str, missing)), {
                    'include': 'participants;coaches;state;scores;events.type;periods.type'})
            fresh = {int(f['id']): slim_fixture(f) for f in response.get('data') or [] if int(f['id']) in missing}
            del response
            if fresh:
                entries = [{'key': f'score-flow-fixture:{SOURCE_VERSION}:{fid}', 'payload': f} for fid, f in fresh.items()]
                db.execute(text('''INSERT INTO enterprise_coach_comparison_cache(cache_key,payload,expires_at)
                    SELECT item->>'key',item->'payload',now()+interval '6 hours'
                    FROM jsonb_array_elements(CAST(:items AS jsonb)) AS item
                    ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,
                        expires_at=EXCLUDED.expires_at,generated_at=now()'''),
                    {'items': json.dumps(entries, separators=(',', ':'))})
                db.commit()
                values.update(fresh)
        yield from (values.get(fid, {'id': fid}) for fid in batch)


def coached_side(fixture, coach_id, teams):
    if fixture.get('timeline') is None:
        return None
    candidates = [tid for tid in teams.get(int(fixture['id']), ()) if any(
        int((c.get('meta') or {}).get('participant_id') or 0) == tid
        and int((c.get('meta') or {}).get('coach_id') or c.get('id') or 0) == coach_id
        for c in fixture.get('coaches') or [])]
    if len(candidates) != 1:
        return None
    return next(((p.get('meta') or {}).get('location') for p in fixture.get('participants') or []
                 if p.get('id') == candidates[0] and (p.get('meta') or {}).get('location') in {'home', 'away'}), None)


def aggregate(fixtures, coach_id, teams):
    seen, included = set(), set()
    transition_matches = {state: {target: 0 for target in ('ahead', 'level', 'behind')}
                          for state in ('ahead', 'level', 'behind')}
    def oriented():
        for fixture in fixtures:
            fid = int(fixture['id'])
            if fid in seen or fid not in teams:
                continue
            seen.add(fid)
            side = coached_side(fixture, coach_id, teams)
            if side is None:
                continue
            included.add(fid)
            data = fixture['timeline']
            # Count each transition at most once per distinct match, even if the
            # team loses and regains the lead repeatedly within the same fixture.
            current, observed, events = 'level', set(), []
            for goal in data['goals']:
                own, other = (goal['home'], goal['away']) if side == 'home' else (goal['away'], goal['home'])
                target = 'ahead' if own > other else 'behind' if own < other else 'level'
                if target != current:
                    observed.add((current, target))
                    current = target
                events.append({'minute': goal['minute'], 'type': {'name': 'Goal'}, 'result': f'{own}-{other}'})
            for origin, target in observed:
                transition_matches[origin][target] += 1
            # The shared model sees one stable, home-oriented coached team ID.
            yield {'id': fid, 'length': data['duration'],
                   'participants': [{'id': 1, 'meta': {'location': 'home'}}], 'events': events}
    result = _aggregate_score_flow(oriented(), 1)
    states = []
    for row in result['states']:
        key = row['key']
        value = {'key': key, 'label': {'ahead': 'Ahead', 'level': 'Level', 'behind': 'Behind'}[key],
                 'minutes': row['minutes'], 'share': row['share']}
        for old, new in [('average_spell', 'averageSpell'), ('segments', 'segments'), ('matches_entered', 'matchesEntered'),
                         ('goals_for', 'goalsFor'), ('goals_against', 'goalsAgainst'), ('to_ahead', 'toAhead'),
                         ('to_level', 'toLevel'), ('to_behind', 'toBehind'), ('final_wins', 'finalWins'),
                         ('final_draws', 'finalDraws'), ('final_losses', 'finalLosses')]:
            value[new] = row[old]
        for target in ('ahead', 'level', 'behind'):
            value['matchesTo' + target.title()] = transition_matches[key][target]
        value['exitsPositive'] = value['toAhead'] + (value['toLevel'] if key == 'behind' else 0)
        value['exitsNegative'] = value['toBehind'] + (value['toLevel'] if key == 'ahead' else 0)
        states.append(value)
    return {'states': states, 'matchCount': len(included)}


def cache_key(prefix, coach_id, teams):
    signature = [[fid, sorted(tids)] for fid, tids in sorted(teams.items())]
    return prefix + ':' + hashlib.sha256(json.dumps([VERSION, coach_id, signature]).encode()).hexdigest()


@history.router.get('/coach-analysis/{coach_id}/score-flow/availability')
def coach_score_flow_availability(coach_id: int = Path(gt=0), user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        data = history.get_periods(db, coach_id)
        periods, teams = resolve_periods(data, [p['id'] for p in data['periods'] if p['eligibleMatchCount'] > 0])
        key = cache_key('coach-score-flow-availability', coach_id, teams)
        with _locks[int(key[-8:], 16) % len(_locks)]:
            value = comparison.read_cache(db, key)
            if value is None:
                included = [int(f['id']) for f in load_fixtures(db, sorted(teams)) if coached_side(f, coach_id, teams) is not None]
                value = {'fixtureIds': included}
                comparison.write_cache(db, key, value, 1)
            included = set(value['fixtureIds'])
            return {'periods': [{'id': p['id'], 'fixtureIds': ids}
                                for p in periods if (ids := sorted(set(p['fixtureIds']) & included))]}
    except comparison.SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None


@history.router.post('/coach-analysis/{coach_id}/score-flow')
def coach_score_flow(body: ScoreFlowRequest, coach_id: int = Path(gt=0), user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        _, teams = resolve_periods(history.get_periods(db, coach_id), body.periodIds)
        key = cache_key('coach-score-flow', coach_id, teams)
        with _locks[int(key[-8:], 16) % len(_locks)]:
            value = comparison.read_cache(db, key)
            if value is None:
                value = aggregate(load_fixtures(db, sorted(teams)), coach_id, teams)
                comparison.write_cache(db, key, value, 6)
            return value
    except comparison.SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None
