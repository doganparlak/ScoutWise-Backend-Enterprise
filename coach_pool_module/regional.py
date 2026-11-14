"""Compact, coach-oriented ball-location distributions over selected fixtures."""
import hashlib
import json
from threading import Lock

from fastapi import Depends, HTTPException, Path
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from match_analysis_module.regional_play import SCOPES, aggregate_regional_play
from . import analysis as history, comparison
from .momentum import MomentumRequest as RegionalRequest, resolve_periods

VERSION = 1
BATCH_SIZE = 5
_locks = [Lock() for _ in range(16)]


def slim_fixture(fixture):
    participants = [{k: p.get(k) for k in ('id', 'meta')} for p in fixture.get('participants') or []]
    home = next((int(p['id']) for p in participants if p.get('id') and (p.get('meta') or {}).get('location') == 'home'), None)
    # The shared team-report calculation normalizes coordinate bounds and uses
    # period IDs for halves. Raw coordinates never enter the persistent cache.
    distribution = aggregate_regional_play((fixture,), home, 'home')['territory_by_period'] if home else {}
    return {'id': int(fixture['id']), 'participants': participants,
            'coaches': [{'id': c.get('id'), 'meta': c.get('meta') or {}} for c in fixture.get('coaches') or []],
            'state': fixture.get('state') or {},
            'counts': {scope: [z['count'] for z in distribution[scope]['zones']] if distribution else [0] * 9 for scope in SCOPES}}


def load_fixtures(db, ids):
    for offset in range(0, len(ids), BATCH_SIZE):
        batch = ids[offset:offset + BATCH_SIZE]
        keys = [f'regional-fixture:{VERSION}:{fid}' for fid in batch]
        rows = db.execute(text('''SELECT payload FROM enterprise_coach_comparison_cache
            WHERE cache_key=ANY(CAST(:keys AS text[])) AND expires_at>now()'''), {'keys': keys}).scalars().all()
        values = {int(row['id']): row for row in rows}
        missing = [fid for fid in batch if fid not in values]
        if missing:
            with history._request_slots:
                response = history._get('fixtures/multi/' + ','.join(map(str, missing)), {
                    'include': 'participants;coaches;state;ballcoordinates;periods.type'})
            fresh = {int(f['id']): slim_fixture(f) for f in response.get('data') or [] if int(f['id']) in missing}
            del response
            if fresh:
                entries = [{'key': f'regional-fixture:{VERSION}:{fid}', 'payload': f} for fid, f in fresh.items()]
                db.execute(text('''INSERT INTO enterprise_coach_comparison_cache(cache_key,payload,expires_at)
                    SELECT item->>'key',item->'payload',now()+interval '6 hours'
                    FROM jsonb_array_elements(CAST(:items AS jsonb)) AS item
                    ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,
                        expires_at=EXCLUDED.expires_at,generated_at=now()'''),
                    {'items': json.dumps(entries, separators=(',', ':'))})
                db.commit()
                values.update(fresh)
        yield from (values.get(fid, {'id': fid}) for fid in batch)


def coached_counts(fixture, coach_id, selected_teams):
    fid = int(fixture['id'])
    participants = {int(p['id']): (p.get('meta') or {}).get('location') for p in fixture.get('participants') or [] if p.get('id')}
    candidates = [tid for tid in selected_teams.get(fid, ()) if any(
        int((c.get('meta') or {}).get('participant_id') or 0) == tid
        and int((c.get('meta') or {}).get('coach_id') or c.get('id') or 0) == coach_id
        for c in fixture.get('coaches') or [])]
    if len(participants) != 2 or set(participants.values()) != {'home', 'away'} or len(candidates) != 1:
        return None
    side = participants.get(candidates[0])
    state = fixture.get('state') or {}
    if side not in {'home', 'away'} or (state.get('short_name') or state.get('state')) not in {'FT', 'AET', 'FT_PEN', 'PEN'}:
        return None
    counts = fixture.get('counts') or {}
    if not sum(counts.get('overall') or []):
        return None
    # Store fixtures once in the home frame; rotate both axes for an away coach.
    return {scope: list(reversed(counts[scope])) if side == 'away' else counts[scope] for scope in SCOPES}


def aggregate(fixtures, coach_id, selected_teams):
    counts = {scope: [0] * 9 for scope in SCOPES}
    covered = {scope: 0 for scope in SCOPES}
    seen = set()
    for fixture in fixtures:
        fid = int(fixture['id'])
        if fid in seen or fid not in selected_teams:
            continue
        seen.add(fid)
        own = coached_counts(fixture, coach_id, selected_teams)
        if own is None:
            continue
        for scope in SCOPES:
            if sum(own[scope]):
                covered[scope] += 1
                counts[scope] = [a + b for a, b in zip(counts[scope], own[scope])]
    scopes = {}
    for scope in SCOPES:
        total = sum(counts[scope])
        scopes[scope] = {'zones': [{'index': i, 'count': value, 'percentage': value / total * 100 if total else 0.}
                                  for i, value in enumerate(counts[scope])],
                         'total': total, 'matchesCovered': covered[scope]}
    return {'scopes': scopes}


def cache_key(prefix, coach_id, teams):
    signature = [[fid, sorted(tids)] for fid, tids in sorted(teams.items())]
    return prefix + ':' + hashlib.sha256(json.dumps([VERSION, coach_id, signature]).encode()).hexdigest()


@history.router.get('/coach-analysis/{coach_id}/regional/availability')
def coach_regional_availability(coach_id: int = Path(gt=0), user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        data = history.get_periods(db, coach_id)
        periods, teams = resolve_periods(data, [p['id'] for p in data['periods'] if p['eligibleMatchCount'] > 0])
        key = cache_key('coach-regional-availability', coach_id, teams)
        with _locks[int(key[-8:], 16) % len(_locks)]:
            value = comparison.read_cache(db, key)
            if value is None:
                included = [int(f['id']) for f in load_fixtures(db, sorted(teams)) if coached_counts(f, coach_id, teams) is not None]
                value = {'fixtureIds': included}
                comparison.write_cache(db, key, value, 1)
            included = set(value['fixtureIds'])
            return {'periods': [{'id': p['id'], 'fixtureIds': ids}
                                for p in periods if (ids := sorted(set(p['fixtureIds']) & included))]}
    except comparison.SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None


@history.router.post('/coach-analysis/{coach_id}/regional')
def coach_regional(body: RegionalRequest, coach_id: int = Path(gt=0),
                   user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        _, teams = resolve_periods(history.get_periods(db, coach_id), body.periodIds)
        key = cache_key('coach-regional', coach_id, teams)
        with _locks[int(key[-8:], 16) % len(_locks)]:
            value = comparison.read_cache(db, key)
            if value is None:
                value = aggregate(load_fixtures(db, sorted(teams)), coach_id, teams)
                comparison.write_cache(db, key, value, 6)
            return value
    except comparison.SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None
