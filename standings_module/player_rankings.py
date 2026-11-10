"""Enterprise-only player leaderboards built entirely from saved match contributions."""
from __future__ import annotations

import json
from threading import Lock
from sqlalchemy import text

from api_module.database import engine
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


def ranked_page(payload, metric='Rating', mode='total', direction=None, offset=0, limit=50):
    catalog = [item for item in payload['metric_catalog'] if item['key'] != 'Match Count']
    selected = next((m for m in catalog if m['key'] == metric), None)
    if selected is None:
        raise ValueError('Unsupported player metric for this league')
    direction = direction or selected['direction']
    field = {'total': 'value', 'perMatch': 'perMatch', 'per90': 'per90'}[mode]
    eligible = [(p['metrics'][metric][field], p, p['metrics'][metric]) for p in payload['players']
                if metric in p['metrics'] and p['metrics'][metric].get(field) is not None]
    eligible.sort(key=lambda item: (item[0] if direction == 'asc' else -item[0],
                                   -item[2]['matchesCovered'], -item[1]['minutes'], item[1]['playerId'], item[1]['teamId']))
    rows = []
    previous = None
    rank = 0
    for index, (value, player, evidence) in enumerate(eligible):
        if previous is None or value != previous:
            rank = index + 1
        previous = value
        if offset <= index < offset + limit:
            row = {k: v for k, v in player.items() if k != 'metrics'}
            row.update(position=rank, awardMetric=metric, awardValue=round(value, 4), awardMode='total' if metric in PLAYER_CONTEXT_METRICS else mode,
                       awardUnit=selected['unit'], awardMatchesCovered=evidence['matchesCovered'],
                       awardMinutesCovered=evidence.get('minutesCovered'))
            if 'denominator' in evidence:
                row.update(awardNumerator=evidence['numerator'], awardDenominator=evidence['denominator'])
            rows.append(row)
    return {'metric_catalog': catalog, 'metric': selected, 'rows': rows,
            'total': len(eligible), 'offset': offset, 'limit': limit, 'direction': direction, 'mode': mode}


def request_rankings(db, league_id, season_id, scope=None, metric='Rating', mode='total', direction=None, offset=0, limit=50, period_start=None):
    ensure_schema()
    scope = scope or ('league' if league_id in EUROPEAN_LEAGUES else 'overall')
    if scope not in SCOPES or (league_id not in EUROPEAN_LEAGUES and scope != 'overall'):
        raise ValueError('Invalid competition scope')
    params = {'league': league_id, 'season': season_id, 'scope': scope, 'version': CACHE_VERSION}
    summary = db.execute(text('''SELECT computed_at,status,coverage FROM league_season_summaries
        WHERE league_id=:league AND season_id=:season'''), params).mappings().first()
    empty = {'status': 'pending', 'refreshing': True, 'metric_catalog': [], 'rows': [], 'total': 0,
             'offset': offset, 'limit': limit, 'computed_at': None, 'scope': scope}
    if summary is None or summary['computed_at'] is None:
        return empty
    saved = db.execute(text('''SELECT payload,source_computed_at,calculation_version FROM enterprise_league_player_rankings
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
        summary = db.execute(text('''SELECT computed_at,status,coverage FROM league_season_summaries
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
        saved = db.execute(text('''SELECT payload,source_computed_at FROM enterprise_league_player_rankings
            WHERE league_id=:league AND season_id=:season AND scope=:scope'''), params).mappings().one()
    if league_id in EUROPEAN_LEAGUES:
        coverage = summary['coverage'] or {}
        periods = ((coverage.get('stageSummaries') or {}).get(scope) or {}).get('periods') or []
    else:
        periods = db.execute(text("""SELECT period_start,period_end,fixture_count,status,computed_at
            FROM league_biweekly_summaries WHERE league_id=:league AND season_id=:season"""), params).mappings().all()
    _, chosen = select_completed_periods(periods, period_start)
    page = ranked_page(saved['payload'], metric, mode, direction, offset, limit)
    best = ranked_page(saved['payload'], metric, mode, limit=1)['rows']
    season_winner = best[0] if best else None
    biweekly_winner = saved['payload'].get('period_awards', {}).get(chosen.isoformat(), {}).get(metric, {}).get(mode) if chosen else None
    players = [*page['rows'], *([season_winner] if season_winner else []), *([biweekly_winner] if biweekly_winner else [])]
    ids = list({p['playerId'] for p in players})
    if ids:
        images = dict(db.execute(text('''SELECT player_id,image_url FROM enterprise_player_images
            WHERE player_id=ANY(CAST(:ids AS bigint[])) AND image_status='available' AND image_url IS NOT NULL'''), {'ids': ids}).all())
        for player in players:
            player['imageUrl'] = images.get(player['playerId']) or player.get('imageUrl')
    team_ids = list({p['teamId'] for p in players})
    if team_ids:
        logos = dict(db.execute(text('''SELECT team_id,image_url FROM enterprise_team_images
            WHERE team_id=ANY(CAST(:ids AS bigint[])) AND image_status='available' AND image_url IS NOT NULL'''), {'ids': team_ids}).all())
        for player in players:
            player['teamImageUrl'] = logos.get(player['teamId'])
    return {**page, 'season_winner': season_winner, 'biweekly_winner': biweekly_winner, 'selectedPeriodStart': chosen, 'status': 'ready', 'refreshing': summary['status'] in {'pending', 'processing'},
            'computed_at': saved['source_computed_at'], 'scope': scope}
