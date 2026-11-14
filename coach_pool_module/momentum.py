"""Coach-oriented momentum over distinct, attributed fixtures with paired pressure."""
import hashlib
import json
import math
import re
from collections import defaultdict
from threading import Lock

from fastapi import Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from . import analysis as history, comparison

VERSION = 1
BATCH_SIZE = 10
_locks = [Lock() for _ in range(16)]


class MomentumRequest(BaseModel):
    periodIds: list[str] = Field(min_length=1, max_length=1000)


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def slim_fixture(fixture):
    return {
        'id': int(fixture['id']),
        'participants': [{k: p.get(k) for k in ('id', 'meta')} for p in fixture.get('participants') or []],
        'coaches': [{'id': c.get('id'), 'meta': c.get('meta') or {}} for c in fixture.get('coaches') or []],
        'state': fixture.get('state') or {},
        'pressure': [{k: r.get(k) for k in ('id', 'participant_id', 'minute', 'pressure')}
                     for r in fixture.get('pressure') or []],
        'events': [{k: r.get(k) for k in ('id', 'participant_id', 'team_id', 'minute', 'extra_minute',
                                         'period_id', 'sort_order', 'result', 'type', 'rescinded')}
                   for r in fixture.get('events') or []],
        'shootoutPeriods': [p['id'] for p in fixture.get('periods') or []
                            if 'shootout' in str(p.get('description') or '').lower().replace('-', '')],
    }


def load_fixtures(db, ids):
    """Bound raw payloads; cache empty pressure too, without changing other sections."""
    for offset in range(0, len(ids), BATCH_SIZE):
        batch = ids[offset:offset + BATCH_SIZE]
        keys = [f'momentum-fixture:{VERSION}:{fid}' for fid in batch]
        rows = db.execute(text('''SELECT payload FROM enterprise_coach_comparison_cache
            WHERE cache_key=ANY(CAST(:keys AS text[])) AND expires_at>now()'''), {'keys': keys}).scalars().all()
        values = {int(row['id']): row for row in rows}
        missing = [fid for fid in batch if fid not in values]
        if missing:
            with history._request_slots:
                response = history._get('fixtures/multi/' + ','.join(map(str, missing)), {
                    'include': 'participants;coaches;state;pressure;events.type;periods'})
            fresh = {int(f['id']): slim_fixture(f) for f in response.get('data') or [] if int(f['id']) in missing}
            del response
            if fresh:
                entries = [{'key': f'momentum-fixture:{VERSION}:{fid}', 'payload': f} for fid, f in fresh.items()]
                db.execute(text('''INSERT INTO enterprise_coach_comparison_cache(cache_key,payload,expires_at)
                    SELECT item->>'key',item->'payload',now()+interval '6 hours'
                    FROM jsonb_array_elements(CAST(:items AS jsonb)) AS item
                    ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,
                        expires_at=EXCLUDED.expires_at,generated_at=now()'''),
                    {'items': json.dumps(entries, separators=(',', ':'))})
                db.commit()
                values.update(fresh)
        # An absent fixture is excluded, never fabricated as a pressure-free match.
        yield from (values.get(fid, {'id': fid}) for fid in batch)


def resolve_periods(data, selected):
    wanted = set(selected)
    periods = [p for p in data['periods'] if p['id'] in wanted]
    if {p['id'] for p in periods} != wanted:
        raise HTTPException(422, 'Choose available coach periods.')
    teams = defaultdict(set)
    for p in periods:
        if p['eligibleMatchCount'] > 0:
            for fid in p['fixtureIds']:
                teams[int(fid)].add(int(p['teamId']))
    return periods, teams


def paired_pressure(fixture, team_id, opponent_id):
    values = {team_id: {}, opponent_id: {}}
    seen = set()
    for row in fixture.get('pressure') or []:
        row_id = row.get('id')
        if row_id is not None:
            if row_id in seen:
                continue
            seen.add(row_id)
        tid = row.get('participant_id')
        minute, value = number(row.get('minute')), number(row.get('pressure'))
        if tid not in values or minute is None or not minute.is_integer() or not 0 <= minute <= 180 or value is None or value < 0:
            continue
        minute = int(minute)
        values[tid][minute] = values[tid].get(minute, 0.) + value
    own, other = values[team_id], values[opponent_id]
    return {minute: own[minute] - other[minute] for minute in own.keys() & other.keys()}


def match_events(fixture, team_id):
    participants = fixture.get('participants') or []
    home = next((p['id'] for p in participants if (p.get('meta') or {}).get('location') == 'home'), None)
    away = next((p['id'] for p in participants if (p.get('meta') or {}).get('location') == 'away'), None)
    score = (0, 0)
    seen = set()
    shootouts = set(fixture.get('shootoutPeriods') or [])
    for row in sorted(fixture.get('events') or [], key=lambda r: (number(r.get('sort_order')) or 0, number(r.get('minute')) or 0)):
        if row.get('rescinded') or row.get('period_id') in shootouts:
            continue
        if row.get('id') is not None:
            if row['id'] in seen:
                continue
            seen.add(row['id'])
        kind = str((row.get('type') or {}).get('code') or (row.get('type') or {}).get('name') or '').lower()
        kind = re.sub(r'[^a-z]', '', kind)
        tid = row.get('participant_id') or row.get('team_id')
        event_kind = None
        if kind in {'goal', 'owngoal', 'penalty', 'penaltygoal'}:
            result = re.fullmatch(r'\s*(\d+)\s*[-:]\s*(\d+)\s*', str(row.get('result') or ''))
            beneficiary = tid if kind != 'owngoal' else None
            if result:
                updated = tuple(map(int, result.groups()))
                delta = (updated[0] - score[0], updated[1] - score[1])
                if delta == (1, 0) and home is not None:
                    beneficiary = home
                elif delta == (0, 1) and away is not None:
                    beneficiary = away
                score = updated
            if beneficiary in {p['id'] for p in participants}:
                event_kind = 'goalFor' if beneficiary == team_id else 'goalAgainst'
        elif kind == 'substitution' and tid == team_id:
            event_kind = 'substitution'
        minute = number(row.get('minute'))
        if event_kind is None or minute is None or not minute.is_integer() or not 0 <= minute <= 180:
            continue
        # Preserve first-half stoppage in its own half. End-of-match added time
        # belongs to the 90+ window; shootout goals are never match goals.
        if minute >= 90:
            minute += max(0, number(row.get('extra_minute')) or 0)
        if minute <= 180:
            yield int(minute), event_kind


def eligible_pressure(fixture, coach_id, selected_teams):
    """Use the same eligibility gate for options and chart aggregation."""
    fid = int(fixture['id'])
    participants = {int(p['id']) for p in fixture.get('participants') or [] if p.get('id')}
    candidates = [tid for tid in selected_teams.get(fid, ()) if any(
        int((c.get('meta') or {}).get('participant_id') or 0) == tid
        and int((c.get('meta') or {}).get('coach_id') or c.get('id') or 0) == coach_id
        for c in fixture.get('coaches') or [])]
    if not participants:
        return None, {}, 'unavailableFixture'
    if len(candidates) != 1 or len(participants) != 2 or candidates[0] not in participants:
        return None, {}, 'coachMismatch'
    state = fixture.get('state') or {}
    if (state.get('short_name') or state.get('state')) not in {'FT', 'AET', 'FT_PEN', 'PEN'}:
        return None, {}, 'notCompleted'
    team_id = candidates[0]
    paired = paired_pressure(fixture, team_id, next(tid for tid in participants if tid != team_id))
    if not paired:
        return None, {}, 'noPairedPressure'
    return team_id, paired, None


def aggregate(fixtures, coach_id, selected_teams):
    totals = defaultdict(lambda: [0., 0])
    events = defaultdict(int)
    included = set()
    seen = set()
    reasons = defaultdict(int)
    for fixture in fixtures:
        fid = int(fixture['id'])
        if fid in seen or fid not in selected_teams:
            continue
        seen.add(fid)
        team_id, paired, reason = eligible_pressure(fixture, coach_id, selected_teams)
        if reason:
            reasons[reason] += 1
            continue
        included.add(fid)
        for minute, value in paired.items():
            totals[minute][0] += value
            totals[minute][1] += 1
        # Events use exactly the same eligible match population as the chart.
        for minute, kind in match_events(fixture, team_id):
            events[minute, kind] += 1
    points = [{'minute': minute, 'value': round(total / count, 4), 'matches': count}
              for minute, (total, count) in sorted(totals.items())]
    event_rows = [{'minute': minute, 'kind': kind, 'count': count} for (minute, kind), count in sorted(events.items())]
    windows = [(0, 10), *[(start, start + 9) for start in range(11, 91, 10)], (91, 180)]
    rows = []
    for start, end in windows:
        scoped = [p for p in points if start <= p['minute'] <= end]
        selected_events = [e for e in event_rows if start <= e['minute'] <= end]
        rows.append({'period': '90+' if start == 91 else f'{start}-{end}',
                     'average': round(sum(p['value'] for p in scoped) / len(scoped), 4) if scoped else None,
                     'dominantShare': round(sum(p['value'] > 0 for p in scoped) / len(scoped) * 100, 1) if scoped else None,
                     'minutesCovered': len(scoped),
                     **{kind: sum(e['count'] for e in selected_events if e['kind'] == kind)
                        for kind in ('goalFor', 'goalAgainst', 'substitution')}})
    return {'points': points, 'events': event_rows, 'windows': rows,
            'coverage': {'requestedMatches': len(selected_teams), 'includedMatches': len(included),
                         'excludedMatches': len(selected_teams) - len(included), 'reasons': dict(reasons)}}, included


@history.router.post('/coach-analysis/{coach_id}/momentum')
def coach_momentum(body: MomentumRequest, coach_id: int = Path(gt=0),
                   user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        data = history.get_periods(db, coach_id)
        periods, teams = resolve_periods(data, body.periodIds)
        signature = [[fid, sorted(tids)] for fid, tids in sorted(teams.items())]
        key = 'coach-momentum:' + hashlib.sha256(json.dumps([VERSION, coach_id, signature]).encode()).hexdigest()
        with _locks[int(key[-8:], 16) % len(_locks)]:
            value = comparison.read_cache(db, key)
            if value is None:
                value, included = aggregate(load_fixtures(db, sorted(teams)), coach_id, teams)
                value['includedFixtureIds'] = sorted(included)
                comparison.write_cache(db, key, value, 1 if value['coverage']['excludedMatches'] else 6)
            # Selection labels can differ while describing the same fixture union.
            included = set(value['includedFixtureIds'])
            return {**{k: v for k, v in value.items() if k != 'includedFixtureIds'},
                    'periods': [{**{k: p.get(k) for k in ('id', 'teamId', 'team', 'teamImageUrl', 'season', 'competition')},
                                 'requestedMatches': len(set(p['fixtureIds'])) if p['eligibleMatchCount'] > 0 else 0,
                                 'includedMatches': len(set(p['fixtureIds']) & included) if p['eligibleMatchCount'] > 0 else 0} for p in periods]}
    except comparison.SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None


@history.router.get('/coach-analysis/{coach_id}/momentum/availability')
def coach_momentum_availability(coach_id: int = Path(gt=0),
                                user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        data = history.get_periods(db, coach_id)
        periods, teams = resolve_periods(data, [p['id'] for p in data['periods'] if p['eligibleMatchCount'] > 0])
        signature = [[fid, sorted(tids)] for fid, tids in sorted(teams.items())]
        key = 'coach-momentum-availability:' + hashlib.sha256(json.dumps([VERSION, coach_id, signature]).encode()).hexdigest()
        with _locks[int(key[-8:], 16) % len(_locks)]:
            cached = comparison.read_cache(db, key)
            if cached is None:
                included = set()
                for fixture in load_fixtures(db, sorted(teams)):
                    _, _, reason = eligible_pressure(fixture, coach_id, teams)
                    if reason is None:
                        included.add(int(fixture['id']))
                cached = {'fixtureIds': sorted(included)}
                comparison.write_cache(db, key, cached, 1)
            included = set(cached['fixtureIds'])
            return {'periods': [{'id': p['id'], 'fixtureIds': ids}
                                for p in periods if (ids := sorted(set(p['fixtureIds']) & included))]}
    except comparison.SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None
