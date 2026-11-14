"""Shared compact evidence for tactics and profiles, keyed by exact fixture union."""
import hashlib
import json
from threading import Lock

from fastapi import HTTPException
from match_analysis_module.pre_match_report import PRE_MATCH_SCHEMA_VERSION
from . import analysis as history, comparison, tactics

VERSION = 5
_locks = [Lock() for _ in range(16)]


def load_period(db, coach_id, team_id, fixture_ids):
    ids = sorted(set(fixture_ids))
    digest = hashlib.sha256(json.dumps([
        VERSION, PRE_MATCH_SCHEMA_VERSION, coach_id, team_id, ids,
    ]).encode()).hexdigest()
    key = 'coach-evidence:' + digest
    # Separate locks: tactics already owns a comparison lock.
    with _locks[int(digest[:8], 16) % len(_locks)]:
        cached = comparison.read_cache(db, key)
        if cached is not None:
            return cached
        value = build_period(db, coach_id, team_id, ids)
        comparison.write_cache(db, key, value, 6)
        return value


def build_period(db, coach_id, team_id, ids):
    from . import profiles

    meta, matches, balance = {}, {}, []
    long_numerator = long_denominator = 0.
    long_covered = 0

    def fixtures():
        nonlocal long_numerator, long_denominator, long_covered
        for fixture in comparison.load_fixtures(db, ids):
            verified = any(
                int((c.get('meta') or {}).get('participant_id') or 0) == team_id
                and int((c.get('meta') or {}).get('coach_id') or c.get('id') or 0) == coach_id
                for c in fixture.get('coaches') or [])
            state = fixture.get('state') or {}
            if (not verified or not history.statistics_eligible(fixture)
                    or (state.get('short_name') or state.get('state')) not in {'FT', 'AET', 'FT_PEN', 'PEN'}):
                raise HTTPException(409, 'Match eligibility has changed. Refresh the coach career.')
            matches[int(fixture['id'])] = profiles.extract_match(fixture, team_id, meta)
            balance.extend(tactics.balance_matches((fixture,), team_id))
            values = {(r.get('type') or {}).get('name'): tactics.numeric((r.get('data') or {}).get('value'))
                      for r in fixture.get('statistics') or [] if int(r.get('participant_id') or 0) == team_id}
            long_balls, passes = values.get('Long Passes', values.get('Long Balls')), values.get('Passes')
            if long_balls is not None and passes is not None and passes > 0 and 0 <= long_balls <= passes:
                long_numerator += long_balls
                long_denominator += passes
                long_covered += 1
            yield fixture

    categories = comparison._aggregate_team_comparison(fixtures(), team_id)
    if long_denominator:
        categories.setdefault('passing', []).append({
            'name': 'Long Ball Share (%)', 'value': round(long_numerator / long_denominator * 100, 2),
            'matches_covered': long_covered, 'aggregation': 'derived_rate',
        })
    players = profiles.PlayerSamples(team_id, meta)
    details = tactics.summarize_details(tactics.load_details(db, ids), team_id, observe=players.add)
    role_samples, player_totals = players.result()
    for fid, sample in matches.items():
        sample.update(role_samples.get(fid, {}))
        profiles.add_context_rates(sample, meta)
    # Store metric names once rather than once per match. Ordinary counts need
    # only a scalar; paired rates retain their exact numerator and denominator.
    columns = {key: [None if key not in sample else
                    sample[key][0] if sample[key][1] == 1 else sample[key]
                    for sample in matches.values()] for key in meta}
    return {'meta': meta, 'columns': columns, 'sampleCount': len(matches), 'players': player_totals,
            'tactics': {'categories': categories, 'balanceMatches': balance, **details}}
