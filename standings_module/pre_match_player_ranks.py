"""Optional pre-match player ranks from saved contributions; never fetch provider data."""
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from sqlalchemy import text
from api_module.database import engine
from .metrics import Aggregate, VERSION, AVERAGES, PLAYER_RATE_METRICS, PLAYER_AWARD_METRICS, LOWER_BETTER, kickoff, numeric, player_metric_result
from .stages import EUROPEAN_LEAGUES

def rank_players(aggregate):
    aggregate.finish(season_awards=True)
    values = {}
    for player in aggregate.players.values():
        matches = aggregate.teams[str(player['teamId'])]['matches']
        if player['ratedAppearances'] and player['appearances'] * 2 >= matches:
            values.setdefault('Rating', []).append((player['ratingSum'] / player['ratedAppearances'], player))
        for metric in PLAYER_AWARD_METRICS:
            result = player_metric_result(player, matches, metric, 'per90')
            if result is not None:
                values.setdefault(metric, []).append((result[0], player))
    ranks = {}
    for metric, candidates in values.items():
        candidates.sort(key=lambda item: (item[0] if metric in LOWER_BETTER else -item[0], item[1]['playerId'], item[1]['teamId']))
        previous = None
        position = 0
        for index, (value, player) in enumerate(candidates):
            if previous is None or value != previous:
                position = index + 1
            previous = value
            ranks.setdefault((player['teamId'], player['playerId']), {})[metric] = {
                'position': position, 'total_players': len(candidates), 'season_value': round(value, 4),
                'basis': 'average_rating' if metric == 'Rating' else 'rate' if metric in PLAYER_RATE_METRICS else 'average' if metric in AVERAGES else 'per90',
                'direction': 'asc' if metric in LOWER_BETTER else 'desc',
            }
    return ranks


@lru_cache(maxsize=24)
def cached_player_ranks(league, season, scope, minute):
    cutoff = datetime.fromtimestamp(minute * 60, timezone.utc)
    aggregate = Aggregate(include_team_metrics=False)
    with engine.connect() as db:
        # Reuse the published leaderboard while a refresh is in progress. A
        # historical report may only use a snapshot published before its cutoff.
        from .player_rankings import CACHE_VERSION
        saved = db.execute(text("""SELECT payload FROM enterprise_league_player_rankings
            WHERE league_id=:league AND season_id=:season AND scope=:scope
            AND calculation_version=:version AND source_computed_at<=:cutoff"""),
            {'league': league, 'season': season, 'scope': scope, 'version': CACHE_VERSION, 'cutoff': cutoff}).scalar()
        if saved:
            values = {}
            for player in saved.get('players', []):
                for metric, evidence in player.get('metrics', {}).items():
                    value = numeric(evidence.get('per90'))
                    if value is not None:
                        values.setdefault(metric, []).append((value, player))
            ranks = {}
            for metric, players in values.items():
                players.sort(key=lambda item: (item[0] if metric in LOWER_BETTER else -item[0], item[1]['playerId'], item[1]['teamId']))
                previous, position = None, 0
                for index, (value, player) in enumerate(players):
                    if previous is None or value != previous:
                        position = index + 1
                    previous = value
                    ranks.setdefault((player['teamId'], player['playerId']), {})[metric] = {
                        'position': position, 'total_players': len(players), 'season_value': round(value, 4),
                        'basis': 'average_rating' if metric == 'Rating' else 'rate' if metric in PLAYER_RATE_METRICS else 'average' if metric in AVERAGES else 'per90',
                        'direction': 'asc' if metric in LOWER_BETTER else 'desc',
                    }
            return ranks
        summary = db.execute(text('''SELECT computed_at,status FROM league_season_summaries
            WHERE league_id=:league AND season_id=:season FOR SHARE NOWAIT'''),
            {'league': league, 'season': season}).mappings().first()
        if summary is None or summary['computed_at'] is None or summary['status'] != 'ready':
            return {}
        rows = db.execute(text('''SELECT kickoff_at,included_in_aggregation,team_contributions,
            player_contributions,calculation_version,coverage FROM league_match_contributions
            WHERE league_id=:league AND season_id=:season AND included_in_aggregation=TRUE
            AND kickoff_at<:cutoff ORDER BY kickoff_at,fixture_id''').execution_options(stream_results=True, yield_per=20),
            {'league': league, 'season': season, 'cutoff': cutoff}).mappings()
        for row in rows:
            if row['calculation_version'] != VERSION:
                return {}
            if league in EUROPEAN_LEAGUES:
                phase = (row['coverage'] or {}).get('scope')
                if phase not in {'league', 'knockout', 'qualifying'}:
                    return {}
                if phase not in ({'qualifying'} if scope == 'qualifying' else {'league', 'knockout'}):
                    continue
            aggregate.add(row)
    return rank_players(aggregate)


def strip_player_ranks(usage):
    """Discard legacy standout-player ranks, retaining team and comparison ranks."""
    result = deepcopy(usage)
    for team in result.get('teams') or []:
        summary = deepcopy(team.get('performance_summary') or {})
        if 'performance_summary' in team:
            team['performance_summary'] = summary
        for key in ('featured_players', 'top_scorers', 'top_rated', 'top_scorer', 'development'):
            entries = summary.get(key)
            for player in entries if isinstance(entries, list) else [entries]:
                if not isinstance(player, dict):
                    continue
                player.pop('rating_competition_rank', None)
                player.pop('league_ranking_highlights', None)
                for group in ('standout_metrics', 'development_metrics'):
                    for metric in player.get(group) or []:
                        metric.pop('competition_rank', None)
    return result
