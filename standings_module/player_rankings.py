"""Enterprise-only player leaderboards built entirely from saved match contributions."""
from __future__ import annotations

import json
import unicodedata
from bisect import bisect_left, bisect_right
from threading import Lock
from datetime import datetime
from sqlalchemy import text

from api_module.database import engine, benchmark_engine
from .metrics import (Aggregate, AVERAGES, LOWER_BETTER, PLAYER_AWARD_METRICS,
                      PLAYER_RATE_METRICS, PLAYER_CONTEXT_METRICS, VERSION, PERIOD_ZONE, period_for, player_metric_result)
from .periods import select_completed_periods
from .stages import EUROPEAN_LEAGUES, SCOPES

CACHE_VERSION = 4
_READY = False
_LOCK = Lock()


def ensure_schema():
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        with engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('enterprise_league_player_rankings_v1',0))"))
            db.execute(text('''CREATE TABLE IF NOT EXISTS public.enterprise_league_player_rankings (
                league_id bigint NOT NULL, season_id bigint NOT NULL, scope text NOT NULL,
                calculation_version integer NOT NULL, source_computed_at timestamptz NOT NULL,
                payload jsonb NOT NULL, PRIMARY KEY (league_id,season_id,scope))'''))
            db.execute(text('ALTER TABLE public.enterprise_league_player_rankings ENABLE ROW LEVEL SECURITY'))
        _READY = True


def snapshot(aggregate):
    """Store each player once, with compact eligible metric values; no network."""
    players = []
    supported = set()
    for player in aggregate.players.values():
        team = aggregate.teams[str(player['teamId'])]
        metrics = {}
        if player['ratedAppearances'] and player['appearances'] * 2 >= team['matches']:
            value = player['ratingSum'] / player['ratedAppearances']
            metrics['Rating'] = {'value': value, 'perMatch': value, 'per90': value, 'matchesCovered': player['ratedAppearances']}
        for name in sorted(PLAYER_AWARD_METRICS):
            result = player_metric_result(player, team['matches'], name)
            if result is None:
                continue
            value, total = result
            per_match, _ = player_metric_result(player, team['matches'], name, 'perMatch')
            per90 = player_metric_result(player, team['matches'], name, 'per90')
            metrics[name] = {'value': value, 'perMatch': per_match, 'per90': per90[0] if per90 else None,
                             'minutesCovered': total.get('minutesCovered'), 'matchesCovered': total['matchesCovered']}
            if name in PLAYER_RATE_METRICS:
                metrics[name].update(numerator=total['numerator'], denominator=total['denominator'])
        if not metrics:
            continue
        supported.update(metrics)
        players.append({k: player.get(k) for k in ('playerId', 'teamId', 'name', 'imageUrl', 'appearances', 'ratedAppearances', 'minutes')} |
                       {'teamName': team['teamName'], 'teamMatches': team['matches'],
                        'averageRating': round(player['ratingSum'] / player['ratedAppearances'], 2) if player['ratedAppearances'] else None,
                        'metrics': metrics})
    catalog = [{'key': 'Rating', 'label': 'Rating', 'aggregation': 'average', 'unit': '', 'direction': 'desc', 'playerAwardSupported': True}]
    for name in sorted(supported - {'Rating'}):
        kind = 'derived_rate' if name in PLAYER_RATE_METRICS else 'average' if name in AVERAGES else 'total'
        catalog.append({'key': name, 'label': name, 'aggregation': kind,
                        'unit': '%' if kind == 'derived_rate' or '%' in name else '',
                        'direction': 'asc' if name in LOWER_BETTER else 'desc', 'playerAwardSupported': True})
    return {'players': players, 'metric_catalog': catalog}


def period_winners(result):
    modes = ('total', 'perMatch', 'per90')
    winners = {'Rating': {mode: result['league_best_player'] for mode in modes}}
    for metric, awards in result['metric_player_awards'].items():
        winners[metric] = {mode: value['league_best_player'] for mode, value in awards.items()}
    return winners


def save_snapshot(db, job, scope, aggregate, source_computed_at=None, period_awards=None):
    db.execute(text('''INSERT INTO enterprise_league_player_rankings
        (league_id,season_id,scope,calculation_version,source_computed_at,payload)
        VALUES (:league,:season,:scope,:version,COALESCE(CAST(:computed AS timestamptz),NOW()),CAST(:payload AS jsonb))
        ON CONFLICT (league_id,season_id,scope) DO UPDATE SET calculation_version=EXCLUDED.calculation_version,
        source_computed_at=EXCLUDED.source_computed_at,payload=EXCLUDED.payload'''),
        {**job, 'scope': scope, 'version': CACHE_VERSION, 'computed': source_computed_at,
         'payload': json.dumps({**snapshot(aggregate), 'period_awards': period_awards or {}}, allow_nan=False)})


PLAYER_PRESETS = {
    'general': ('Rating', 'Goals', 'Assists', 'Key Passes', 'Tackles', 'Interceptions'),
    'attack': ('Goals', 'Expected Goals', 'Assists', 'Shots Total', 'Shots On Target', 'Big Chances Created'),
    'defense': ('Tackles', 'Interceptions', 'Ball Recovery', 'Clearances', 'Aerials Won'),
}


def preset_page(payload, preset, metric, direction, offset, limit, team_id, column_keys=None, *, mode="per90", query="", cell_context=False, averages=False, standouts=False):
    catalog = {item['key']: item for item in payload['metric_catalog'] if item['key'] != 'Match Count'}
    defaults = [key for key in PLAYER_PRESETS[preset] if key in catalog]
    keys = [key for key in dict.fromkeys(column_keys or defaults) if key in catalog] or defaults
    columns = [catalog[key] for key in keys]
    if not columns:
        return {'metric_catalog': payload['metric_catalog'], 'columns': [], 'defaultColumns': defaults, 'rows': [],
                'total': 0, 'offset': offset, 'limit': limit}
    if metric not in {column['key'] for column in columns}:
        metric, direction = columns[0]['key'], None
    def normalize(value):
        return ''.join(char for char in unicodedata.normalize('NFKD', value.casefold().replace('ı', 'i'))
                       if not unicodedata.combining(char))

    search = normalize(query.strip())
    field = {'total': 'value', 'perMatch': 'perMatch', 'per90': 'per90'}[mode]
    universe = payload['players']
    players = [player for player in universe if (team_id is None or player['teamId'] == team_id)
               and (not search or search in normalize(f"{player['name']} {player['teamName']}"))]
    page = ranked_page({**payload, 'players': players}, metric, mode, direction, offset, limit, include_zero=True)
    page.update(columns=columns, defaultColumns=defaults)
    metrics_by_player = {(player['playerId'], player['teamId']): player['metrics'] for player in players}
    distributions = {}
    if cell_context or averages:
        for column in columns:
            key = column['key']
            distributions[key] = sorted(value for player in universe
                                        if (value := player['metrics'].get(key, {}).get(field)) is not None)
    if averages:
        page['averages'] = {key: sum(values) / len(values) for key, values in distributions.items() if values}
    for row in page['rows']:
        samples = metrics_by_player[row['playerId'], row['teamId']]
        row['presetValues'] = {key: samples.get(key, {}).get(field) for key in keys}
        if cell_context:
            row['metricContext'] = {}
            for column in columns:
                key = column['key']
                value, values = row['presetValues'][key], distributions[key]
                if value is None or not values:
                    continue
                left, right = bisect_left(values, value), bisect_right(values, value)
                better = left if column['direction'] == 'asc' else len(values) - right
                weaker = len(values) - right if column['direction'] == 'asc' else left
                row['metricContext'][key] = {'rank': better + 1, 'percentile': round(100 * (weaker + (right - left) / 2) / len(values))}
    if standouts:
        leaders = ranked_page(payload, metric, mode, None, 0, 3, include_zero=True, standout_team=team_id)
        page['standouts'] = {'leaders': leaders['rows'], 'clubBest': leaders.get('clubBest')}

    return page


def ranked_page(payload, metric='Rating', mode='total', direction=None, offset=0, limit=50, *, include_zero=False, standout_team=None):
    catalog = [item for item in payload['metric_catalog'] if item['key'] != 'Match Count']
    selected = next((m for m in catalog if m['key'] == metric), None)
    if selected is None:
        raise ValueError('Unsupported player metric for this league')
    direction = direction or selected['direction']
    field = {'total': 'value', 'perMatch': 'perMatch', 'per90': 'per90'}[mode]
    eligible = [(p['metrics'][metric][field], p, p['metrics'][metric]) for p in payload['players']
                if metric in p['metrics'] and (value := p['metrics'][metric].get(field)) is not None
                and (include_zero or round(value, 4) != 0)]
    eligible.sort(key=lambda item: (item[0] if direction == 'asc' else -item[0],
                                   -item[2]['matchesCovered'], -item[1]['minutes'], item[1]['playerId'], item[1]['teamId']))
    rows = []
    previous = None
    club_best = None
    rank = 0
    for index, (value, player, evidence) in enumerate(eligible):
        if previous is None or value != previous:
            rank = index + 1
        previous = value
        on_page = offset <= index < offset + limit
        is_club_best = standout_team is not None and club_best is None and player['teamId'] == standout_team
        if on_page or is_club_best:
            row = {k: v for k, v in player.items() if k != 'metrics'}
            row.update(position=rank, awardMetric=metric, awardValue=round(value, 4), awardMode='total' if metric in PLAYER_CONTEXT_METRICS else mode,
                       awardUnit=selected['unit'], awardMatchesCovered=evidence['matchesCovered'],
                       awardMinutesCovered=evidence.get('minutesCovered'))
            if 'denominator' in evidence:
                row.update(awardNumerator=evidence['numerator'], awardDenominator=evidence['denominator'])
            if on_page:
                rows.append(row)
            if is_club_best:
                club_best = row
        if index + 1 >= offset + limit and (standout_team is None or club_best is not None):
            break
    return {'metric_catalog': catalog, 'metric': selected, 'rows': rows,
            'total': len(eligible), 'offset': offset, 'limit': limit, 'direction': direction, 'mode': mode, 'clubBest': club_best}


def request_rankings(db, league_id, season_id, scope=None, metric='Rating', mode='total', direction=None, offset=0, limit=50, period_start=None, *, preset=None, team_id=None, columns=None, query="", cell_context=False, averages=False, standouts=False, export_rows=False):
    # Schema is initialized at startup/migration time, never during a read request.
    scope = scope or ('league' if league_id in EUROPEAN_LEAGUES else 'overall')
    if scope not in SCOPES or (league_id not in EUROPEAN_LEAGUES and scope != 'overall'):
        raise ValueError('Invalid competition scope')
    params = {'league': league_id, 'season': season_id, 'scope': scope, 'version': CACHE_VERSION}
    if preset:
        current = db.execute(text("""SELECT ranks.payload, ranks.source_computed_at, summary.status
            FROM enterprise_league_player_rankings ranks
            JOIN league_season_summaries summary USING (league_id, season_id)
            WHERE ranks.league_id=:league AND ranks.season_id=:season AND ranks.scope=:scope
              AND ranks.calculation_version=:version AND ranks.source_computed_at=summary.computed_at"""),
            params).mappings().first()
        if current is not None:
            page = preset_page(current['payload'], preset, metric, direction, offset, limit, team_id, columns,
                               mode=mode, query=query, cell_context=cell_context, averages=averages, standouts=standouts)
            page.update(season_winner=None, biweekly_winner=None, source_computed_at=current['source_computed_at'].isoformat())
            return finalize_page(db, page, scope, current['status'], None, export_rows)
    summary = db.execute(text('''SELECT computed_at,status,(SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'period_start',p->'period_start','period_end',p->'period_end',
                'fixture_count',p->'fixture_count','status',COALESCE(p->'status','"ready"'::jsonb),
                'computed_at',p->'computed_at')),'[]'::jsonb)
                FROM jsonb_array_elements(COALESCE(coverage->'stageSummaries'->:scope->'periods','[]'::jsonb)) p) AS scoped_periods FROM league_season_summaries
        WHERE league_id=:league AND season_id=:season'''), params).mappings().first()
    empty = {'status': 'pending', 'refreshing': True, 'metric_catalog': [], 'rows': [], 'total': 0,
             'offset': offset, 'limit': limit, 'computed_at': None, 'scope': scope}
    if summary is None or summary['computed_at'] is None:
        return empty
    saved = db.execute(text('''SELECT source_computed_at,calculation_version FROM enterprise_league_player_rankings
        WHERE league_id=:league AND season_id=:season AND scope=:scope AND calculation_version>=2'''), params).mappings().first()
    rebuild = saved is None or saved['source_computed_at'] != summary['computed_at'] or saved['calculation_version'] != CACHE_VERSION
    if rebuild and summary['status'] != 'ready' and saved is None:
        return {**empty, 'status': 'failed' if summary['status'] == 'failed' else 'pending', 'refreshing': summary['status'] != 'failed'}
    # Keep the last complete player table visible during additive field backfills.
    if rebuild and summary['status'] == 'ready':
        # Serialize first materialization per league/scope. Existing team summaries
        # stay intact, and this path never calls SportMonks or changes refresh dates.
        acquired = db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))'),
                              {'key': f'enterprise-player-rankings:{league_id}:{season_id}:{scope}'}).scalar()
        if not acquired:
            return empty
        summary = db.execute(text('''SELECT computed_at,status,(SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'period_start',p->'period_start','period_end',p->'period_end',
                'fixture_count',p->'fixture_count','status',COALESCE(p->'status','"ready"'::jsonb),
                'computed_at',p->'computed_at')),'[]'::jsonb)
                FROM jsonb_array_elements(COALESCE(coverage->'stageSummaries'->:scope->'periods','[]'::jsonb)) p) AS scoped_periods FROM league_season_summaries
            WHERE league_id=:league AND season_id=:season FOR SHARE'''), params).mappings().one()
        if summary['status'] != 'ready':
            return empty
        aggregate = Aggregate(include_team_metrics=False)
        fortnight = Aggregate(include_team_metrics=False)
        current_period = None
        period_awards = {}
        for row in db.execute(text('''SELECT kickoff_at,included_in_aggregation,team_contributions,player_contributions,
            calculation_version,coverage FROM league_match_contributions
            WHERE league_id=:league AND season_id=:season AND included_in_aggregation=TRUE
            ORDER BY kickoff_at,fixture_id''').execution_options(stream_results=True, yield_per=20), params).mappings():
            if row['calculation_version'] != VERSION:
                return empty
            if league_id in EUROPEAN_LEAGUES:
                phase = (row['coverage'] or {}).get('scope')
                if phase not in {'league', 'qualifying', 'knockout'}:
                    return empty
                if phase not in ({'league', 'knockout'} if scope == 'overall' else {scope}):
                    continue
            period = period_for(row['kickoff_at'].astimezone(PERIOD_ZONE).date())[0]
            if current_period is not None and current_period != period:
                period_awards[current_period.isoformat()] = period_winners(fortnight.finish())
                fortnight = Aggregate(include_team_metrics=False)
            current_period = period
            fortnight.add(row)
            aggregate.add(row)
        if current_period is not None:
            period_awards[current_period.isoformat()] = period_winners(fortnight.finish())
        aggregate.finish(season_awards=True)  # Finalize pooled rates and missing-attempt evidence.
        save_snapshot(db, params, scope, aggregate, summary['computed_at'], period_awards)
        db.commit()
        saved = db.execute(text('''SELECT source_computed_at FROM enterprise_league_player_rankings
            WHERE league_id=:league AND season_id=:season AND scope=:scope'''), params).mappings().one()
    chosen = None
    if preset:
        saved_payload = db.execute(text("""SELECT payload, source_computed_at FROM enterprise_league_player_rankings
            WHERE league_id=:league AND season_id=:season AND scope=:scope"""), params).mappings().first()
        if saved_payload is None:
            return empty
        snapshot_payload = saved_payload['payload']
        page = preset_page(snapshot_payload, preset, metric, direction, offset, limit, team_id, columns,
                           mode=mode, query=query, cell_context=cell_context, averages=averages, standouts=standouts)
        page['season_winner'] = None
        page['biweekly_winner'] = None
        page['source_computed_at'] = saved_payload['source_computed_at'].isoformat()
    else:
        if league_id in EUROPEAN_LEAGUES:
            periods = summary['scoped_periods'] or []
        else:
            periods = db.execute(text("""SELECT period_start,period_end,fixture_count,status,computed_at
                FROM league_biweekly_summaries WHERE league_id=:league AND season_id=:season"""), params).mappings().all()
        _, chosen = select_completed_periods(periods, period_start)
        # Snapshot values are normalized once in Supabase; return only this page.
        with benchmark_engine.begin() as ranking_db:
            ready = ranking_db.execute(text("SELECT public.scoutwise_ensure_league_rank_rows_v1(:league,:season,:scope)"), params).scalar_one()
            if not ready:
                return empty
            page = ranking_db.execute(text("""SELECT public.scoutwise_league_player_page_v1(
                :league,:season,:scope,:metric,:mode,:direction,:offset,:limit,:period)"""),
                {**params, 'metric': metric, 'mode': mode, 'direction': direction, 'offset': offset,
                 'limit': limit, 'period': chosen.isoformat() if chosen else None}).scalar_one()
    if page.get('error') == 'unsupported_metric':
        raise ValueError('Unsupported player metric for this league')
    if page.get('error'):
        return empty
    return finalize_page(db, page, scope, summary['status'], chosen, export_rows and bool(preset))


def finalize_page(db, page, scope, summary_status, chosen, export_rows):
    season_winner = page.pop('season_winner')
    biweekly_winner = page.pop('biweekly_winner')
    source_computed_at = datetime.fromisoformat(page.pop('source_computed_at'))
    players = [*page['rows'], *([season_winner] if season_winner else []), *([biweekly_winner] if biweekly_winner else [])]
    if export_rows:
        players = []
    if players:
        ids = {p['playerId'] for p in players}
        team_ids = {p['teamId'] for p in players}
        images = {(kind, entity_id): url for kind, entity_id, url in db.execute(text("""
            SELECT 'player', player_id, image_url FROM enterprise_player_images
            WHERE player_id=ANY(CAST(:ids AS bigint[])) AND image_status='available' AND image_url IS NOT NULL
            UNION ALL
            SELECT 'team', team_id, image_url FROM enterprise_team_images
            WHERE team_id=ANY(CAST(:teams AS bigint[])) AND image_status='available' AND image_url IS NOT NULL
        """), {'ids': list(ids), 'teams': list(team_ids)}).all()}
        for player in players:
            player['imageUrl'] = images.get(('player', player['playerId'])) or player.get('imageUrl')
            player['teamImageUrl'] = images.get(('team', player['teamId']))
    return {**page, 'season_winner': season_winner, 'biweekly_winner': biweekly_winner, 'selectedPeriodStart': chosen, 'status': 'ready', 'refreshing': summary_status in {'pending', 'processing'},
            'computed_at': source_computed_at, 'scope': scope}
