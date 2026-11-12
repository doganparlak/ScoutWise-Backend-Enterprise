"""Optional pre-match player ranks from saved contributions; never fetch provider data."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from sqlalchemy import text
from api_module.database import benchmark_engine
from .metrics import Aggregate, VERSION
from .stages import EUROPEAN_LEAGUES


def cached_player_ranks(league, season, scope, minute, mode="per90", *, players):
    """Return only requested ranks; shared SQL caches never retain league maps in RAM."""
    from .player_rankings import CACHE_VERSION, snapshot
    from .metrics import _hide_incomplete_attempts
    targets = sorted({(int(team), int(player)) for team, player in players})
    if not targets:
        return {}
    if mode not in {'total', 'perMatch', 'per90'}:
        raise ValueError('Unsupported ranking mode')
    cutoff = datetime.fromtimestamp(minute * 60, timezone.utc)
    args = dict(l=league, s=season, sc=scope, cutoff=cutoff, version=CACHE_VERSION,
                targets=json.dumps(targets), mode=mode)

    def selected(db):
        result = db.execute(text("""SELECT scoutwise_selected_player_ranks_v1(
            :l,:s,:sc,CAST(:targets AS jsonb),:mode,:cutoff)"""), args).scalar()
        return None if result is None else {(r['team_id'], r['player_id']): r['metrics'] for r in result}

    with benchmark_engine.begin() as db:
        saved = db.execute(text("""SELECT 1 FROM enterprise_league_player_rankings
            WHERE league_id=:l AND season_id=:s AND scope=:sc
            AND calculation_version=:version AND source_computed_at<=:cutoff"""), args).scalar()
        if saved:
            db.execute(text('SELECT scoutwise_ensure_league_rank_rows_v1(:l,:s,:sc)'), args)
            db.execute(text('SELECT scoutwise_ensure_league_rank_distributions_v1(:l,:s,:sc)'), args)
            result = selected(db)
            if result is not None:
                return result
        summary = db.execute(text("""SELECT computed_at,status FROM league_season_summaries
            WHERE league_id=:l AND season_id=:s FOR SHARE NOWAIT"""), args).mappings().first()
        if not summary or summary['computed_at'] is None or summary['status'] != 'ready':
            return {}
        # Fingerprint the exact historical input, including corrections, without
        # downloading player contributions on a cache hit.
        fingerprint = db.execute(text("""SELECT md5(COALESCE(string_agg(
            concat_ws(':',fixture_id,calculation_version,extract(epoch FROM fetched_at),
                source_fingerprint,coverage->>'scope'),',' ORDER BY kickoff_at,fixture_id),''))
            FROM league_match_contributions WHERE league_id=:l AND season_id=:s
            AND included_in_aggregation=TRUE AND kickoff_at<:cutoff"""), args).scalar()
        args['sc'] = f'pre_match:{scope}:{minute}:{fingerprint}'
        db.execute(text("""SELECT pg_advisory_xact_lock(hashtextextended(
            'scoutwise-pre-match-history:'||CAST(:l AS text)||':'||CAST(:s AS text)||':'||:sc,0))"""), args)
        exists = db.execute(text("""SELECT 1 FROM scoutwise_league_rank_meta_v1
            WHERE league_id=:l AND season_id=:s AND scope=:sc AND calculation_version=:version"""), args).scalar()
        if not exists:
            aggregate = Aggregate(include_team_metrics=False)
            rows = db.execute(text("""SELECT kickoff_at,included_in_aggregation,team_contributions,
                player_contributions,calculation_version,coverage FROM league_match_contributions
                WHERE league_id=:l AND season_id=:s AND included_in_aggregation=TRUE
                AND kickoff_at<:cutoff ORDER BY kickoff_at,fixture_id""").execution_options(stream_results=True, yield_per=20), args).mappings()
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
            for player in aggregate.players.values():
                _hide_incomplete_attempts(player['metricTotals'], player.get('_rate_inputs') or {})
            args['payload'] = json.dumps(snapshot(aggregate))
            del aggregate
            db.execute(text("""SELECT scoutwise_store_league_rank_rows_v1(
                :l,:s,:sc,:cutoff,:version,CAST(:payload AS jsonb))"""), args)
            del args['payload']
        db.execute(text("""UPDATE scoutwise_league_rank_meta_v1 SET last_used_at=NOW()
            WHERE league_id=:l AND season_id=:s AND scope=:sc
            AND last_used_at<NOW()-INTERVAL '5 minutes'"""), args)
        result = selected(db)
        db.execute(text('SELECT scoutwise_trim_pre_match_histories_v1(24)'))
        return result or {}


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
