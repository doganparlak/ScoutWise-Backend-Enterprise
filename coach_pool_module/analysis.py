"""Full recorded career with bounded, batch-based match eligibility checks."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import hashlib
import math
import re
import os
import threading
import time

import requests
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from match_analysis_module.match_analysis import SportMonksError

router = APIRouter()
SCHEMA_VERSION = 5
MINIMUM_MATCHES = 10
CORE_STATS = {'ball-possession', 'shots-total', 'shots-on-target', 'passes', 'successful-passes'}
CACHE_BYTES_LIMIT = 8 * 1024 * 1024
_cache = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()
_refresh_locks = [threading.Lock() for _ in range(16)]
_request_slots = threading.BoundedSemaphore(3)


def _date(value):
    try:
        return date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def coaching_spells(profile, today):
    spells = {}
    for appointment in profile.get('teams') or []:
        tid = appointment.get('team_id')
        if not tid or (appointment.get('position_id') and appointment['position_id'] != 221):
            continue
        start, end = _date(appointment.get('start')), _date(appointment.get('end'))
        if start and start > today:
            continue
        last = min(end or today, today)
        if start and start > last:
            continue
        key = f'{tid}:{start or "unknown"}:{end or "open"}'
        team = appointment.get('team') or {}
        spells[key] = {'id': key, 'teamId': int(tid), 'team': team.get('name') or str(tid),
                       'teamImageUrl': team.get('image_path'), 'source': 'sportmonks-appointment', 'start': start.isoformat() if start else '',
                       'end': end.isoformat() if end else '', 'active': bool(appointment.get('active')),
                       '_first': start, '_last': last}
    return sorted(spells.values(), key=lambda row: (row['active'], row['_last'], row['_first'] or date.min), reverse=True)


def _get(path, params):
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        raise SportMonksError('ScoutWise data service is not configured')
    base = os.getenv('SPORTMONKS_BASE_URL', 'https://api.sportmonks.com/v3/football').rstrip('/')
    try:
        response = requests.get(f'{base}/{path}', headers={'Authorization': token}, params=params, timeout=30)
        if not response.ok:
            raise SportMonksError(f'Coach match history could not be loaded (HTTP {response.status_code})')
        return response.json()
    except (requests.RequestException, ValueError):
        raise SportMonksError('Coach match history is temporarily unavailable') from None


def load_career_evidence(db, coach_id, record, metrics):
    fetched = record.get('career_fetched_at')
    if record.get('career_data') is not None and fetched and datetime.now(timezone.utc) - fetched < timedelta(days=1):
        return record['career_data'], str(fetched)
    with _request_slots:
        payload = _get(f'coaches/{coach_id}', {'include': 'statistics.team;statistics.season'})
        metrics['upstreamRequests'] += 1
    evidence = []
    for row in (payload.get('data') or {}).get('statistics') or []:
        season = row.get('season') or {}
        if row.get('team_id') and _date(season.get('starting_at')) and _date(season.get('ending_at')):
            team = row.get('team') or {}
            evidence.append({'teamId': int(row['team_id']), 'team': team.get('name') or str(row['team_id']),
                'imageUrl': team.get('image_path'), 'season': season.get('name') or '',
                'first': season['starting_at'], 'last': season['ending_at']})
    fetched = datetime.now(timezone.utc)
    db.execute(text('''UPDATE enterprise_coach_images SET career_data=CAST(:data AS jsonb),
        career_fetched_at=:fetched WHERE coach_id=:id'''),
        {'id': coach_id, 'data': json.dumps(evidence), 'fetched': fetched})
    db.commit()
    return evidence, str(fetched)


def complete_career_spells(profile, evidence, today):
    # Sportmonks season evidence supplies search windows, never appointment dates.
    spells = coaching_spells(profile, today)
    candidates = {}
    for row in evidence:
        first, last = _date(row.get('first')), _date(row.get('last'))
        if not first or not last or first > today:
            continue
        last = min(last, today)
        tid = row['teamId']
        if any(s['teamId'] == tid and (s['_first'] or date.min) <= last and s['_last'] >= first for s in spells):
            continue
        key = (tid, row.get('season') or f'{first}:{last}')
        candidate = candidates.setdefault(key, dict(row, _first=first, _last=last))
        candidate['_first'] = min(candidate['_first'], first)
        candidate['_last'] = max(candidate['_last'], last)
    for (tid, season), row in candidates.items():
        spells.append({'id': f"{tid}:season-history:{season}", 'teamId': tid, 'team': row['team'],
            'teamImageUrl': row.get('imageUrl'), 'start': '', 'end': '', 'active': False,
            'source': 'sportmonks-season', 'historySeason': season, '_first': row['_first'], '_last': row['_last']})
    return sorted(spells, key=lambda row: (row['active'], row['_last'], row['_first'] or date.min), reverse=True)


def resolve_unknown_starts(spells, metrics):
    starts = {}
    for spell in spells:
        if spell['_first']:
            continue
        tid = spell['teamId']
        if tid not in starts:
            with _request_slots:
                payload = _get(f'teams/{tid}', {'include': 'seasons'})
                metrics['upstreamRequests'] += 1
            seasons = (payload.get('data') or {}).get('seasons') or []
            dates = [_date(s.get('starting_at')) for s in seasons]
            starts[tid] = min((d for d in dates if d), default=None)
        # No invented cutoff or inferred appointment date; missing coverage remains unselectable.
        spell['_first'] = starts[tid]


def request_windows(spells):
    by_team = {}
    for spell in spells:
        if spell['_first'] and spell['_first'] <= spell['_last']:
            by_team.setdefault(spell['teamId'], []).append((spell['_first'], spell['_last']))
    windows = []
    for tid, intervals in by_team.items():
        merged = []
        for first, last in sorted(intervals):
            if merged and first <= merged[-1][1] + timedelta(days=1):
                merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
            else:
                merged.append((first, last))
        for first, last in merged:
            while first <= last:
                end = min(last, first + timedelta(days=99))
                windows.append((tid, first, end))
                first = end + timedelta(days=1)
    return windows


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def statistics_eligible(fixture):
    participants = {int(p['id']) for p in fixture.get('participants') or [] if p.get('id')}
    if len(participants) != 2:
        return False
    values = {pid: {} for pid in participants}
    for stat in fixture.get('statistics') or []:
        pid = stat.get('participant_id')
        code = (stat.get('type') or {}).get('code')
        value = _number((stat.get('data') or {}).get('value'))
        if pid in values and code in CORE_STATS and value is not None:
            values[pid][code] = value
    for stats in values.values():
        if not CORE_STATS <= stats.keys():
            return False
        if stats['ball-possession'] > 100 or stats['shots-on-target'] > stats['shots-total'] or stats['successful-passes'] > stats['passes']:
            return False
    scores = {}
    for score in fixture.get('scores') or []:
        if score.get('description') == 'CURRENT':
            goals = _number((score.get('score') or {}).get('goals'))
            pid = score.get('participant_id')
            if pid in participants and goals is not None and goals.is_integer():
                scores[pid] = goals
    return scores.keys() == participants


def fixture_scores(fixture):
    values = {}
    for score in fixture.get('scores') or []:
        if score.get('description') != 'CURRENT':
            continue
        value = _number((score.get('score') or {}).get('goals'))
        if score.get('participant_id') and value is not None and value.is_integer():
            values[int(score['participant_id'])] = int(value)
    return values


def fixture_result(fixture, team_id):
    scores = fixture.get('_scores') or {}
    participants = {int(p['id']) for p in fixture.get('participants') or [] if p.get('id')}
    if len(participants) != 2 or team_id not in participants or set(scores) != participants:
        return None
    # Shootout decisions do not turn a drawn match into a 3-point win.
    state = fixture.get('state') or {}
    if (state.get('short_name') or state.get('state')) in {'FT_PEN', 'PEN'}:
        return 'draws'
    own, other = scores[team_id], scores[next(pid for pid in participants if pid != team_id)]
    return 'wins' if own > other else 'losses' if own < other else 'draws'


def compact_fixture(fixture):
    # Raw statistics and type descriptions are discarded before any page is retained.
    return {**{k: fixture.get(k) for k in ('id', 'starting_at', 'league_id', 'season_id', 'state')},
        'participants': [{'id': p.get('id')} for p in fixture.get('participants') or []],
        'coaches': [{'id': c.get('id'), 'meta': c.get('meta') or {}} for c in fixture.get('coaches') or []],
        'league': {k: (fixture.get('league') or {}).get(k) for k in ('id', 'name', 'image_path', 'type', 'country')},
        'season': {k: (fixture.get('season') or {}).get(k) for k in ('id', 'name')},
        '_statisticsEligible': statistics_eligible(fixture), '_scores': fixture_scores(fixture)}


def fetch_fixtures(window):
    tid, first, last = window
    rows = []
    for page in range(1, 101):
        with _request_slots:
            payload = _get(f'fixtures/between/{first.isoformat()}/{last.isoformat()}/{tid}',
                {'include': 'coaches;league.country;season;state;participants;scores;statistics.type:code',
                 'per_page': 50, 'page': page, 'filters': 'fixtureStatisticTypes:42,45,80,81,86'})
            rows.extend(compact_fixture(f) for f in payload.get('data') or [])
            has_more = (payload.get('pagination') or {}).get('has_more')
            del payload
        if not has_more:
            return tid, rows, page
    raise SportMonksError('Coach match history exceeded the pagination limit')


def apply_latest_season_default(history):
    groups = {}
    for p in history.get('periods') or []:
        if p['eligibleMatchCount'] > 0:
            key = (p['teamId'], p.get('season') or p.get('seasonId'))
            groups.setdefault(key, []).append(p)
    suitable = [rows for rows in groups.values() if len({fid for p in rows for fid in p['fixtureIds']}) >= 5]
    def recent(rows):
        years = re.findall(r'\d{4}', rows[0].get('season') or '')
        return (int(years[0]) if years else 0, max(p.get('lastMatch') or '' for p in rows))
    chosen = sorted(suitable, key=recent, reverse=True)[:5]
    return {**history, 'defaultSelectedPeriodIds': [p['id'] for rows in chosen for p in rows]}


def group_periods(coach_id, spells, fixtures, today):
    groups, seen = {}, set()
    metrics = {'upstreamRequests': 0, 'dateWindows': 0}
    for tid, rows, requests_count in fixtures:
        metrics['upstreamRequests'] += requests_count
        metrics['dateWindows'] += 1
        for fixture in rows:
            fid = fixture.get('id')
            if not fid or (tid, fid) in seen:
                continue
            seen.add((tid, fid))
            played = _date(fixture.get('starting_at'))
            state = fixture.get('state') or {}
            if not played or played > today or (state.get('short_name') or state.get('state')) not in {'FT', 'AET', 'FT_PEN', 'PEN'}:
                continue
            matching = [s for s in spells if s['teamId'] == tid and s['_first'] and s['_first'] <= played <= s['_last']]
            if not matching or not any(int(p.get('id') or 0) == tid for p in fixture.get('participants') or []):
                continue
            coaches = [c for c in fixture.get('coaches') or [] if int((c.get('meta') or {}).get('participant_id') or 0) == tid]
            verified = any(int((c.get('meta') or {}).get('coach_id') or c.get('id') or 0) == coach_id for c in coaches)
            spell = max(matching, key=lambda s: s['_first'])
            league, season = fixture.get('league') or {}, fixture.get('season') or {}
            league_id, season_id = fixture.get('league_id') or league.get('id'), fixture.get('season_id') or season.get('id')
            key = f"{spell['id']}:{league_id or 0}:{season_id or 0}"
            group = groups.setdefault(key, {
                'id': key, 'spellId': spell['id'], 'teamId': tid, 'team': spell['team'], 'teamImageUrl': spell['teamImageUrl'],
                'appointmentStart': spell['start'], 'appointmentEnd': spell['end'],
                'leagueId': league_id, 'competition': league.get('name') or '', 'competitionImageUrl': league.get('image_path'),
                'competitionType': league.get('type') or '', 'country': (league.get('country') or {}).get('name') or '',
                'seasonId': season_id, 'season': season.get('name') or '', 'firstMatch': played.isoformat(),
                'lastMatch': played.isoformat(), 'fixtureIds': [], 'matchCount': 0, 'eligibleMatchCount': 0,
                'results': {'wins': 0, 'draws': 0, 'losses': 0}})
            group['matchCount'] += 1
            outcome = fixture_result(fixture, tid) if verified else None
            if outcome:
                group['results'][outcome] += 1
            if verified and fixture.get('_statisticsEligible'):
                group['fixtureIds'].append(int(fid))
                group['eligibleMatchCount'] += 1
            group['firstMatch'] = min(group['firstMatch'], played.isoformat())
            group['lastMatch'] = max(group['lastMatch'], played.isoformat())
    periods = sorted(groups.values(), key=lambda g: (g['lastMatch'], g['team'], g['competition']), reverse=True)
    latest = spells[0]['id'] if spells else None
    first = min((s['_first'] for s in spells if s['_first']), default=None)
    public_spells = [{k: v for k, v in s.items() if not k.startswith('_')} for s in spells]
    return apply_latest_season_default({'schemaVersion': SCHEMA_VERSION, 'minimumMatches': MINIMUM_MATCHES,
            'coachId': coach_id, 'since': first.isoformat() if first else '', 'through': today.isoformat(),
            'latestSpellId': latest, 'periods': periods, 'spells': public_spells, 'careerSpells': public_spells,
            'defaultSelectedPeriodIds': [p['id'] for p in periods if p['spellId'] == latest and p['eligibleMatchCount'] > 0],
            'requests': metrics})


def _fixture_batches(windows):
    # At most three windows are in flight/retained, even for a decades-long career.
    with ThreadPoolExecutor(max_workers=3) as executor:
        for offset in range(0, len(windows), 3):
            yield from executor.map(fetch_fixtures, windows[offset:offset + 3])


def source_fingerprint(profile, evidence):
    source = {'teams': profile.get('teams') or [], 'evidence': evidence,
              'schema': SCHEMA_VERSION, 'minimumMatches': MINIMUM_MATCHES, 'coreStats': sorted(CORE_STATS)}
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_persistent_history(db, coach_id, fingerprint, today):
    row = db.execute(text('''SELECT payload, expires_at FROM enterprise_coach_analysis_cache
        WHERE coach_id=:id AND schema_version=:schema AND source_fingerprint=:fingerprint
          AND through_date=:today AND expires_at > now()'''),
        {'id': coach_id, 'schema': SCHEMA_VERSION, 'fingerprint': fingerprint, 'today': today}).mappings().one_or_none()
    if row is None:
        return None
    return row['payload'], row['expires_at']


def save_persistent_history(db, coach_id, fingerprint, today, result):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=6)
    db.execute(text('''INSERT INTO enterprise_coach_analysis_cache
        (coach_id, schema_version, source_fingerprint, through_date, payload, generated_at, expires_at)
        VALUES (:id, :schema, :fingerprint, :today, CAST(:payload AS jsonb), :generated, :expires)
        ON CONFLICT (coach_id) DO UPDATE SET schema_version=EXCLUDED.schema_version,
            source_fingerprint=EXCLUDED.source_fingerprint, through_date=EXCLUDED.through_date,
            payload=EXCLUDED.payload, generated_at=EXCLUDED.generated_at, expires_at=EXCLUDED.expires_at
        WHERE enterprise_coach_analysis_cache.generated_at <= EXCLUDED.generated_at'''),
        {'id': coach_id, 'schema': SCHEMA_VERSION, 'fingerprint': fingerprint, 'today': today,
         'payload': json.dumps(result, separators=(',', ':')), 'generated': now, 'expires': expires})
    db.commit()
    return expires


def remember_history(coach_id, fingerprint, today, result, persistent_expiry):
    global _cache_bytes
    encoded = json.dumps(result, separators=(',', ':')).encode()
    size = len(encoded)
    # A memory hit must never extend the persistent cache's freshness window.
    ttl = min(1800, max(0, (persistent_expiry - datetime.now(timezone.utc)).total_seconds()))
    with _cache_lock:
        previous = _cache.pop(coach_id, None)
        if previous:
            _cache_bytes -= len(previous[3])
        if size <= CACHE_BYTES_LIMIT and ttl > 0:
            while _cache and (_cache_bytes + size > CACHE_BYTES_LIMIT or len(_cache) >= 8):
                _, expired = _cache.popitem(last=False)
                _cache_bytes -= len(expired[3])
            _cache[coach_id] = (time.monotonic() + ttl, fingerprint, today.isoformat(), encoded)
            _cache_bytes += size


def get_periods(db, coach_id):
    today = date.today()
    with _refresh_locks[coach_id % len(_refresh_locks)]:
        record = db.execute(text('SELECT profile_data, updated_at, career_data, career_fetched_at FROM enterprise_coach_images WHERE coach_id=:id'), {'id': coach_id}).mappings().one_or_none()
        if record is None:
            raise HTTPException(404, 'Coach profile not found')
        extra = {'upstreamRequests': 0}
        evidence, _ = load_career_evidence(db, coach_id, record, extra)
        version = source_fingerprint(record['profile_data'], evidence)
        with _cache_lock:
            cached = _cache.get(coach_id)
            if cached and cached[0] > time.monotonic() and cached[1] == version and cached[2] == today.isoformat():
                _cache.move_to_end(coach_id)
                return apply_latest_season_default(json.loads(cached[3]))
        saved = load_persistent_history(db, coach_id, version, today)
        if saved:
            result, expires = saved
            result = apply_latest_season_default(result)
            remember_history(coach_id, version, today, result, expires)
            return result
        spells = complete_career_spells(record['profile_data'], evidence, today)
        resolve_unknown_starts(spells, extra)
        windows = request_windows(spells)
        result = group_periods(coach_id, spells, _fixture_batches(windows), today)
        result['requests']['upstreamRequests'] += extra['upstreamRequests']
        expires = save_persistent_history(db, coach_id, version, today, result)
        remember_history(coach_id, version, today, result, expires)
        return result


@router.get('/coach-analysis/{coach_id}/periods')
def coach_analysis_periods(coach_id: int = Path(gt=0), user_id: str = Depends(require_auth), db=Depends(get_db)):
    try:
        return get_periods(db, coach_id)
    except SportMonksError as exc:
        raise HTTPException(502, str(exc)) from None
