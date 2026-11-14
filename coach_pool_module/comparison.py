"""Coach comparisons reuse the pre-match formulas over verified fixture unions."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from match_analysis_module.match_analysis import SportMonksError
from match_analysis_module.pre_match_report import _aggregate_team_comparison, PRE_MATCH_SCHEMA_VERSION
from . import analysis as history

VERSION = 1
MINIMUM_COMPARISON_MATCHES = 5
_locks = [Lock() for _ in range(32)]
_maintenance_lock = Lock()
_last_cleanup = 0.0


class ComparisonScope(BaseModel):
    periodIds: list[str] = Field(min_length=1, max_length=1000)


class ComparisonRequest(BaseModel):
    slots: list[ComparisonScope] = Field(min_length=2, max_length=2)


def prune_expired_cache(db):
    global _last_cleanup
    now=time.monotonic()
    if now-_last_cleanup<300 or not _maintenance_lock.acquire(blocking=False):return
    try:
        if now-_last_cleanup<300:return
        db.execute(text("""DELETE FROM enterprise_coach_comparison_cache WHERE cache_key IN
            (SELECT cache_key FROM enterprise_coach_comparison_cache WHERE expires_at<now() ORDER BY expires_at LIMIT 200)"""))
        db.commit();_last_cleanup=now
    finally:_maintenance_lock.release()


def read_cache(db, key):
    return db.execute(text('SELECT payload FROM enterprise_coach_comparison_cache WHERE cache_key=:key AND expires_at>now()'), {'key': key}).scalar_one_or_none()


def write_cache(db, key, value, hours):
    prune_expired_cache(db)
    db.execute(text('''INSERT INTO enterprise_coach_comparison_cache(cache_key,payload,expires_at)
        VALUES (:key,CAST(:payload AS jsonb),:expires) ON CONFLICT(cache_key) DO UPDATE
        SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at,generated_at=now()'''),
        {'key': key, 'payload': json.dumps(value, separators=(',', ':')), 'expires': datetime.now(timezone.utc) + timedelta(hours=hours)})
    db.commit()


def resolve_scope(data, scope):
    ids = set(scope.periodIds)
    periods = [p for p in data['periods'] if p['id'] in ids and p['eligibleMatchCount'] > 0]
    if {p['id'] for p in periods} != ids or len({p['teamId'] for p in periods}) != 1:
        raise HTTPException(422, 'Choose eligible periods from one team for each comparison slot.')
    fixtures = sorted({fid for p in periods for fid in p['fixtureIds']})
    if len(fixtures) < MINIMUM_COMPARISON_MATCHES:
        raise HTTPException(422, f"Each comparison slot requires {MINIMUM_COMPARISON_MATCHES} eligible matches.")
    return periods[0]['teamId'], fixtures


def slim_fixture(fixture):
    value = {key: fixture.get(key) for key in ('id', 'starting_at', 'league_id', 'season_id', 'state_id', 'state', 'coaches', 'scores')}
    value['coaches'] = [{'id': row.get('id'), 'meta': row.get('meta') or {}} for row in fixture.get('coaches') or []]
    value['participants'] = [{key: p.get(key) for key in ('id', 'meta')} for p in fixture.get('participants') or []]
    for field, source in [('statistics', fixture.get('statistics')), ('xgfixture', fixture.get('xgfixture') or fixture.get('xGFixture'))]:
        value[field] = [{'participant_id': row.get('participant_id'), 'type_id': row.get('type_id'),
            'type': {key: (row.get('type') or {}).get(key) for key in ('id', 'name', 'code')},
            'data': {'value': (row.get('data') or {}).get('value')}} for row in source or []]
    value['events'] = [{key: event.get(key) for key in ('id', 'participant_id', 'team_id', 'rescinded', 'type')}
        for event in fixture.get('events') or [] if (event.get('type') or {}).get('name', '').casefold() in {'penalty', 'missed penalty'}]
    return value


def load_fixtures(db, fixture_ids):
    # Only one batch of provider payloads is decoded at a time. No player includes.
    for offset in range(0, len(fixture_ids), 50):
        batch = fixture_ids[offset:offset + 50]
        keys = [f'fixture:{VERSION}:{fid}' for fid in batch]
        cached = db.execute(text('''SELECT cache_key,payload FROM enterprise_coach_comparison_cache
            WHERE cache_key=ANY(CAST(:keys AS text[])) AND expires_at>now()'''), {'keys': keys}).mappings().all()
        values = {int(row['payload']['id']): row['payload'] for row in cached}
        missing = [fid for fid in batch if fid not in values]
        if missing:
            with history._request_slots:
                response = history._get('fixtures/multi/' + ','.join(map(str, missing)), {
                    'include': 'participants;coaches;state;scores;statistics.type;xGFixture.type;events.type'})
            received = {int(f['id']): slim_fixture(f) for f in response.get('data') or [] if int(f['id']) in missing}
            del response
            if set(received) != set(missing):
                raise SportMonksError('Some comparison matches are unavailable. Please try again.')
            entries = [{'key': f'fixture:{VERSION}:{fid}', 'payload': fixture} for fid, fixture in received.items()]
            db.execute(text('''INSERT INTO enterprise_coach_comparison_cache(cache_key,payload,expires_at)
                SELECT item->>'key',item->'payload',:expires FROM jsonb_array_elements(CAST(:items AS jsonb)) AS item
                ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at,generated_at=now()'''),
                {'items': json.dumps(entries, separators=(',', ':')), 'expires': datetime.now(timezone.utc) + timedelta(hours=24)})
            db.commit()
            values.update(received)
        yield from (values[fid] for fid in batch)


def build_scope(db, coach_id, team_id, fixture_ids):
    digest = hashlib.sha256(json.dumps([VERSION, PRE_MATCH_SCHEMA_VERSION, coach_id, team_id, fixture_ids]).encode()).hexdigest()
    key = f'scope:{digest}'
    with _locks[int(digest[:8], 16) % len(_locks)]:
        cached = read_cache(db, key)
        if cached is not None:
            return cached
        fixtures = list(load_fixtures(db, fixture_ids))
        for fixture in fixtures:
            verified = any(int((c.get('meta') or {}).get('participant_id') or 0) == team_id
                and int((c.get('meta') or {}).get('coach_id') or c.get('id') or 0) == coach_id for c in fixture.get('coaches') or [])
            state = fixture.get('state') or {}
            if not verified or not history.statistics_eligible(fixture) or (state.get('short_name') or state.get('state')) not in {'FT', 'AET', 'FT_PEN', 'PEN'}:
                raise HTTPException(409, 'Match eligibility has changed. Refresh the coach career and try again.')
        result = {'teamId': team_id, 'matchCount': len(fixtures), 'categories': _aggregate_team_comparison(fixtures, team_id)}
        write_cache(db, key, result, 6)
        return result


@history.router.post('/coach-analysis/{coach_id}/comparison')
def compare_coach_scopes(body: ComparisonRequest, coach_id: int = Path(gt=0), user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        data = history.get_periods(db, coach_id)
        scopes = [resolve_scope(data, scope) for scope in body.slots]
        return {'slots': [build_scope(db, coach_id, team_id, ids) for team_id, ids in scopes]}
    except SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None
