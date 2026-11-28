"""Score one match against the competition's pre-kickoff player profiles."""
from collections import defaultdict

from sqlalchemy import text

from report_module.percentiles import MIN_PEERS, NEGATIVE, rank
from report_module.rate_evidence import add_sample, empty, key as rate_key, rate_value
from standings_module.metrics import AVERAGES, PLAYER_COUNT_METRICS, kickoff, numeric
from standings_module.stages import EUROPEAN_LEAGUES, stage_scope
from team_portfolio_module.percentiles import MIN_MINUTES, _RATE_LIMITS, build_profiles

VOLUME_METRICS = {rate_key(name): name for name in PLAYER_COUNT_METRICS - AVERAGES}


def match_values(lineup):
    # Reports contain canonicalized fixture detail and nullable provider data.
    observations = {}
    sources = (*(lineup.get('categories') or {}).values(), lineup.get('expected_metrics') or [],
               lineup.get('extra_metrics') or [])
    for rows in sources:
        for item in rows:
            name = item.get('name')
            if not name or item.get('_inferred_zero') or isinstance(item.get('value'), bool):
                continue
            if name == 'Blocked Shots' and item.get('source_name') != 'Shots Blocked':
                continue  # Match the standings exclusion of the shooter's blocked attempts.
            value = numeric(item.get('value'))
            if value is not None and value >= 0:
                observations.setdefault(rate_key(name), (name, value))
    minutes = observations.get('minutes_played', ('', 0))[1]
    if minutes <= 0:
        return {}
    metrics = {}
    for key, (_, value) in observations.items():
        if name := VOLUME_METRICS.get(key):
            metrics[name] = {'matchValue': value * 90 / minutes, 'basis': 'per90', 'lowerIsBetter': name in NEGATIVE}
    evidence = empty()
    add_sample(evidence, {name: value for name, value in observations.values()})
    for name, minimum in _RATE_LIMITS.items():
        state = evidence['rates'].get(rate_key(name))
        value = rate_value(name, state)
        if value is not None and state['denominator'] >= minimum:
            metrics[name] = {'matchValue': value, 'basis': 'percentage', 'lowerIsBetter': False}
    rating = observations.get('rating', ('', 0))[1]
    if 0 < rating <= 10:
        metrics['Rating'] = {'matchValue': rating, 'basis': 'rating', 'lowerIsBetter': False}
    return metrics


def score_fixture_players(lineups, profiles):
    subjects = []
    requested = defaultdict(set)
    for player in lineups:
        pid = player.get('player_id')
        profile = profiles.get(pid)
        if not profile or profile['minutes'] < MIN_MINUTES:
            continue
        values = match_values(player)
        if not values:
            continue
        subjects.append((pid, profile, values))
        for group in profile['comparisonGroups']:
            requested[profile['gender'], group].update(values)

    distributions = defaultdict(list)
    for profile in profiles.values():
        if profile['minutes'] < MIN_MINUTES:
            continue
        for group in profile['comparisonGroups']:
            cohort = (profile['gender'], group)
            for name in requested.get(cohort, set()).intersection(profile['metrics']):
                distributions[(*cohort, name)].append(round(profile['metrics'][name]['value'], 6))
    for values in distributions.values():
        values.sort()

    result = {}
    for pid, profile, current in subjects:
        groups = {}
        for group, info in profile['comparisonGroups'].items():
            metrics = {}
            for name, value in current.items():
                pool = distributions.get((profile['gender'], group, name), [])
                percentile = rank(round(value['matchValue'], 6), pool, value['lowerIsBetter'])
                if percentile is not None:
                    metrics[name] = {**value, 'percentile': percentile, 'peerCount': len(pool)}
            if metrics:
                groups[group] = {**info, 'metrics': metrics}
        if groups:
            result[str(pid)] = {'groups': groups, 'selectedGroup': next(iter(groups))}
    return result


def match_player_percentiles(db, content, fixture_payload):
    fixture = content.get('fixture') or {}
    league = (content.get('league') or {}).get('id') or fixture.get('league_id') or fixture_payload.get('league_id')
    season = (content.get('season') or {}).get('id') or fixture.get('season_id') or fixture_payload.get('season_id')
    starting_at = fixture.get('starting_at') or fixture_payload.get('starting_at')
    base = {'status': 'unavailable', 'minimumPeers': MIN_PEERS, 'minimumSeasonMinutes': MIN_MINUTES, 'players': {}}
    if not league or not season or not starting_at:
        return base
    cutoff = kickoff(starting_at)
    if not cutoff:
        return base
    league, season = int(league), int(season)
    european = league in EUROPEAN_LEAGUES
    scope = 'qualifying' if european and stage_scope(content.get('stage') or fixture_payload.get('stage') or {}) == 'qualifying' else 'overall'
    params = {'league': league, 'season': season, 'cutoff': cutoff, 'european': european, 'scope': scope}
    positions = db.execute(text('SELECT player_id,gender,position_counts FROM player_comp_data WHERE league_id=:league'), params).mappings().all()
    if not positions:
        return base
    rows = db.execute(text("""SELECT player_contributions FROM league_match_contributions
        WHERE league_id=:league AND season_id=:season AND included_in_aggregation=TRUE AND kickoff_at<:cutoff
        AND EXISTS (SELECT 1 FROM league_season_summaries s
            WHERE s.league_id=:league AND s.season_id=:season AND s.computed_at IS NOT NULL
            AND (:european=FALSE OR (s.coverage->'stageSummaries') ? :scope))
        AND (:european=FALSE OR (:scope='qualifying' AND coverage->>'scope'='qualifying') OR
             (:scope='overall' AND coverage->>'scope' IN ('league','knockout')))""")
        .execution_options(stream_results=True, yield_per=20), params).mappings()
    try:
        profiles = build_profiles(rows, positions)
    finally:
        rows.close()
    if not profiles:
        return base
    players = score_fixture_players(content.get('lineups') or [], profiles)
    return {**base, 'status': 'ready', 'players': players}
