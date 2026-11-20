"""League benchmarks for the lineup-derived dimensions in a match profile."""
from collections import Counter, defaultdict

from sqlalchemy import text

from api_module.database import SessionLocal
from standings_module.comparison_ranks import rank_metrics
from standings_module.stages import EUROPEAN_LEAGUES


def lineup_style_ranks(league_id, season_id, cutoff, scope):
    from coach_pool_module import profiles, tactics

    with SessionLocal() as db:
        fixtures = db.execute(text("""
            SELECT fixture_id,home_team_id,away_team_id,
                   (team_contributions->CAST(home_team_id AS text)->'metrics'->>'Passes')::float8 AS home_passes,
                   (team_contributions->CAST(away_team_id AS text)->'metrics'->>'Passes')::float8 AS away_passes
            FROM league_match_contributions
            WHERE league_id=:league AND season_id=:season AND kickoff_at<:cutoff
                AND included_in_aggregation=TRUE AND (:european=FALSE OR
                    (:scope='qualifying' AND coverage->>'scope'='qualifying') OR
                    (:scope='overall' AND coverage->>'scope' IN ('league','knockout')))
            ORDER BY kickoff_at,fixture_id
        """), dict(league=league_id, season=season_id, cutoff=cutoff, scope=scope,
                   european=league_id in EUROPEAN_LEAGUES)).mappings().all()
        schedule = {row['fixture_id']: row for row in fixtures}
        played = Counter(tid for row in fixtures for tid in (row['home_team_id'], row['away_team_id']))
        if sum(count >= 3 for count in played.values()) < 5:
            return {}

        meta = {}
        totals = defaultdict(dict)
        for fixture in tactics.load_details(db, list(schedule)):
            row = schedule[fixture['id']]
            for tid, opponent_passes in ((row['home_team_id'], row['away_passes']), (row['away_team_id'], row['home_passes'])):
                players = [p for p in fixture['players'] if p['teamId'] == tid]
                roles = [(p, tactics.role_name(p)) for p in players]
                # Require a substantially complete lineup with known positions.
                if (not any(role == 'goalkeeper' for _, role in roles)
                        or sum(p['minutes'] for p, role in roles if role != 'unknown') < 800):
                    continue
                pass_coverage = sum('Passes' in p['metrics'] and role not in {'goalkeeper', 'unknown'}
                                    for p, role in roles)
                accumulator = profiles.PlayerSamples(tid, meta)
                accumulator.add({**fixture, 'players': players})
                sample = accumulator.samples[fixture['id']]
                if opponent_passes is not None:
                    sample['Opponent Passes'] = (opponent_passes, 1)
                profiles.add_context_rates(sample, meta)
                for key, (numerator, denominator) in sample.items():
                    if not key.startswith(('role:', 'Outfield ')):
                        continue
                    # Pass-share denominators need recorded passing for most outfield starters.
                    if (key in {'role:defender:Passes', 'role:midfielder:Passes', 'role:attacker:Passes',
                                'Outfield Final-third Pass Share (%)'} and pass_coverage < 8):
                        continue
                    aggregate = totals[tid].setdefault(key, [0, 0, 0])
                    aggregate[0] += numerator
                    aggregate[1] += denominator
                    aggregate[2] += 1

    teams = {}
    for tid, metrics in totals.items():
        comparable = {}
        for key, (numerator, denominator, covered) in metrics.items():
            if not denominator or covered < max(3, (played[tid] + 1) // 2):
                continue
            value = numerator / denominator * (100 if meta[key]['percentage'] else 1)
            comparable[key] = {'perMatch': value, 'matchesCovered': covered}
        teams[str(tid)] = {'matches': played[tid], 'metrics': comparable}
    return rank_metrics(teams)
