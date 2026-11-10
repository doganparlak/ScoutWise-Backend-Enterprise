"""Enterprise player comparisons over the report's exact selected fixtures."""
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from datetime import datetime, timezone
import re
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from api_module.database import engine
from match_report_module.report import DERIVED_PERCENTAGE_METRICS, PLAYER_METRIC_CATEGORY
from standings_module.metrics import Aggregate, VERSION, AVERAGES, PLAYER_RATE_METRICS, LOWER_BETTER, kickoff, numeric
from standings_module.pre_match_player_ranks import cached_player_ranks
from standings_module.stages import EUROPEAN_LEAGUES, stage_scope

_CATEGORIES = {canonical: category for category, canonical in PLAYER_METRIC_CATEGORY.values()}
# Derived rates inherit their underlying player's category instead of falling
# back to Contribution & Impact. Matchup Center uses the same grouping.
_CATEGORIES.update({name: _CATEGORIES.get(numerator, 'contribution_impact')
                    for name, (numerator, _) in DERIVED_PERCENTAGE_METRICS.items()})
for _category, _names in {
    'shooting': ('Hit Woodwork', 'On-Target Shot Quality (%)', 'On-Target to Goal Conversion (%)'),
    'passing': ('Through Balls', 'Through Balls Won'),
    'goalkeeping': ('Penalties Saved', 'Penalty Saves'),
    'contribution_impact': ('Penalties Won',),
    'errors_discipline': ('Penalties Committed', 'Penalties Missed', 'Turn Over', 'Own Goals', 'Yellow & Red Cards'),
    'defending': ('Last Man Tackle', 'Clearance Offline'),
}.items():
    _CATEGORIES.update(dict.fromkeys(_names, _category))



def metric_category(name):
    if name in {'Saves','Saves Insidebox','Punches','Good High Claim','Penalty Saves'}:
        return 'goalkeeping'
    if name.startswith('Expected ') or name == 'Shooting Performance':
        return 'expected'
    category = _CATEGORIES.get(name, 'contribution_impact')
    if category == 'set_pieces':
        return 'shooting' if any(term in name.lower() for term in ('goal', 'shot', 'penalt')) else 'passing'
    if name in LOWER_BETTER:
        return 'errors_discipline'
    return category


def comparison_player(player):
    return {key: player.get(key) for key in ('player_id','player_name','player_image_url','position_id','position_name','minutes','appearances','average_rating')} | {
        'metrics': [{**metric, 'category': metric_category(metric['name']),
                     'lower_is_better': metric['name'] in LOWER_BETTER or metric_category(metric['name']) == 'errors_discipline'}
                    for metric in player.get('per90_metrics') or []]}



def append_comparison_role_metrics(players, fixtures, team_id):
    """Keep keeper and xG counts available without changing standout-player selection."""
    totals = {}
    for fixture in fixtures:
        for row in fixture.get('lineups') or []:
            if int(row.get('team_id') or 0) != int(team_id):
                continue
            details = row.get('details') or []
            def value(detail):
                data = detail.get('data')
                return numeric(data.get('value') if isinstance(data, dict) and 'value' in data else detail.get('value', data))
            minutes = next((value(detail) for detail in details if (detail.get('type') or {}).get('name') == 'Minutes Played'), None)
            if not minutes or minutes <= 0:
                continue
            seen = set()
            for detail in [*(row.get('xglineup') or row.get('xGLineup') or []), *details]:
                raw_name = (detail.get('type') or {}).get('name')
                category, name = PLAYER_METRIC_CATEGORY.get(raw_name, ('',''))
                name = {'Expected Goals (xG)':'Expected Goals','Expected Goals':'Expected Goals','Expected Goals on Target (xGoT)':'Expected Goals On Target','Expected Goals On Target':'Expected Goals On Target','Expected Assists (xA)':'Expected Assists','Expected Assists':'Expected Assists'}.get(raw_name,name)
                amount = value(detail)
                if (category != 'goalkeeping' and name not in {'Saves','Goals Conceded','Expected Goals','Expected Goals On Target','Expected Assists'}) or not name or '%' in name or amount is None or amount < 0 or name in seen:
                    continue
                seen.add(name)
                key = (int(row.get('player_id') or 0), name)
                item = totals.setdefault(key, {'sum':0,'minutes':0,'matches':0})
                item['sum'] += amount; item['minutes'] += minutes; item['matches'] += 1
    for player in players:
        existing = {m['name'] for m in player.get('per90_metrics') or []}
        for (pid, name), item in totals.items():
            if pid == player['player_id'] and name not in existing and item['minutes'] >= 90:
                player['per90_metrics'].append({'name':name,'value':round(item['sum']*90/item['minutes'],2),
                    'is_percentage':False,'matches_covered':item['matches'],'recorded_minutes':item['minutes']})

def cached_selected_players(usage, fixture):
    """Old snapshots are enriched only if every selected fixture is cached."""
    ids = list(dict.fromkeys(usage.get('selected_fixture_ids') or [r.get('fixture_id') for r in usage.get('results') or []]))
    ids = [int(fid) for fid in ids if fid]
    if not ids or len(ids) != usage.get('sample_size'):
        return None
    team_id = int(usage['team_id'])
    with engine.connect() as db:
        rows = db.execute(text('''SELECT fixture_id,kickoff_at,included_in_aggregation,team_contributions,
            player_contributions,calculation_version FROM league_match_contributions
            WHERE fixture_id=ANY(CAST(:ids AS bigint[]))
            AND included_in_aggregation=TRUE AND kickoff_at<:cutoff ORDER BY kickoff_at,fixture_id'''),
            {'ids': ids, 'cutoff': datetime.now(timezone.utc)}).mappings().all()
    if len(rows) != len(ids) or any(row['calculation_version'] != VERSION or str(team_id) not in row['team_contributions'] for row in rows):
        return None
    aggregate = Aggregate(include_team_metrics=False)
    for row in rows:
        aggregate.add(row)
    aggregate.finish()
    identities = {int(p['player_id']): p for p in usage.get('players') or []}
    result = []
    for player in aggregate.players.values():
        if player['teamId'] != team_id or player['playerId'] not in identities:
            continue
        metrics = []
        for name, total in player['metricTotals'].items():
            if name in PLAYER_RATE_METRICS:
                if total.get('rateValue') is not None:
                    metrics.append({'name':name,'value':total['rateValue'],'is_percentage':True,'matches_covered':total['matchesCovered']})
            elif total.get('minutesCovered', 0) >= 90 and not total.get('missingMinutes'):
                metrics.append({'name':name,'value':round(total['sum']/total['matchesCovered'] if name in AVERAGES else total['sum']*90/total['minutesCovered'],2),'is_percentage':False,
                                'recorded_minutes':total['minutesCovered'],'matches_covered':total['matchesCovered']})
        identity = identities[player['playerId']]
        result.append(comparison_player({**identity, 'minutes':player['minutes'],'appearances':player['appearances'],
            'average_rating':identity.get('average_rating'), 'per90_metrics':metrics}))
    return result


def enrich_player_comparison(usage, fixture):
    if not (fixture.get('league') or {}).get('id') or not (fixture.get('season') or {}).get('id'):
        return usage
    existing = usage.get('player_comparison')
    try:
        windows = {int(team['team_id']): set(map(int, team.get('selected_fixture_ids') or
                   [row['fixture_id'] for row in team.get('results') or []])) for team in usage.get('teams') or []}
        # Older comparison payloads contain only the competition subset. Reuse
        # them only when they already cover exactly the saved selected matches.
        valid_existing = existing and all(
            set(map(int, group.get('selected_fixture_ids') or [])) == windows.get(int(group['team_id']), set())
            and group.get('sample_size') == len(windows.get(int(group['team_id']), set()))
            for group in existing.get('teams') or []) and len(existing.get('teams') or []) == len(windows)
        if valid_existing:
            data = deepcopy(existing)
        else:
            teams = []
            for team in usage.get('teams') or []:
                players = cached_selected_players(team, fixture)
                teams.append({'team_id':team['team_id'],'sample_size':team.get('sample_size',0),
                              'selected_fixture_ids':team.get('selected_fixture_ids') or [r.get('fixture_id') for r in team.get('results') or []],
                              'players':players or [],'status':'ready' if players is not None else 'unavailable'})
            data = {'teams':teams}
        data.update(stats_scope='selected_matches', league_id=int(fixture['league']['id']), season_id=int(fixture['season']['id']),
                    competition_name=fixture['league'].get('name') or '', season_name=fixture['season'].get('name') or '')
        current = datetime.now(timezone.utc)
        ranks = {}
        cutoff = min(current, kickoff(fixture.get('startingAt') or fixture.get('starting_at')))
        if cutoff:
            scope = 'qualifying' if int(fixture['league']['id']) in EUROPEAN_LEAGUES and stage_scope(fixture.get('stage') or {}) == 'qualifying' else 'overall'
            try:
                ranks = cached_player_ranks(int(fixture['league']['id']),int(fixture['season']['id']),scope,int(cutoff.timestamp())//60)
            except SQLAlchemyError:
                pass  # Selected-match comparisons remain usable without league ranks.
        for team in data['teams']:
            saved_team = next((item for item in usage.get('teams') or [] if item['team_id'] == team['team_id']), {})
            summary = saved_team.get('performance_summary') or {}
            saved_players = {}
            for group in ('featured_players', 'top_scorers', 'top_rated', 'top_scorer', 'development'):
                entries = summary.get(group)
                for player in entries if isinstance(entries, list) else [entries]:
                    if isinstance(player, dict):
                        saved_players[int(player['player_id'])] = player
            for player in team.get('players') or []:
                # Keep the report's frozen selected-match evidence identical
                # between standout cards and this comparison, including older
                # snapshots reconstructed from compact match contributions.
                saved = saved_players.get(int(player['player_id']))
                if saved:
                    recorded = saved.get('per90_metrics') or [*(saved.get('standout_metrics') or []), *(saved.get('development_metrics') or [])]
                    canonical = {metric['name']: metric for metric in comparison_player({'per90_metrics': recorded})['metrics']}
                    player['metrics'] = list({**{metric['name']: metric for metric in player.get('metrics') or []}, **canonical}.values())
                    for key in ('minutes', 'appearances', 'average_rating'):
                        if saved.get(key) is not None:
                            player[key] = saved[key]
                evidence = ranks.get((int(team['team_id']),int(player['player_id'])),{})
                if evidence.get('Rating'):
                    player['rating_competition_rank'] = dict(evidence['Rating'])
                for metric in player.get('metrics') or []:
                    # Reclassify saved snapshots as well as newly generated data;
                    # values and league rank evidence remain untouched.
                    metric['category'] = metric_category(metric['name'])
                    metric['lower_is_better'] = metric['name'] in LOWER_BETTER or metric['category'] == 'errors_discipline'
                    if evidence.get(metric['name']):
                        metric['competition_rank'] = dict(evidence[metric['name']])
        return {**usage,'player_comparison':data}
    except (SQLAlchemyError, ValueError, KeyError, TypeError):
        return usage


# Share a one-time fill across concurrent opens in the same backend process.
_PENDING = {}
_PENDING_LOCK = Lock()


def prepare_saved_comparison(key, usage, fixture):
    with _PENDING_LOCK:
        future = _PENDING.get(key)
        owner = future is None
        if owner:
            future = _PENDING[key] = Future()
    if not owner:
        return deepcopy(future.result(timeout=180))
    try:
        from .pre_match_report import _fixture_lineup_usage, _team_recent_squad_usage
        enriched = enrich_player_comparison(usage, fixture)
        data = enriched.get('player_comparison') or {'teams':[]}
        missing = [team for team in usage.get('teams') or []
                   if not any(group['team_id'] == team['team_id'] and group.get('status') == 'ready' for group in data['teams'])]
        ids_by_team = {}
        for team in missing:
            ids = list(dict.fromkeys(team.get('selected_fixture_ids') or [r.get('fixture_id') for r in team.get('results') or []]))
            if not ids or len(ids) != team.get('sample_size'):
                raise ValueError('The saved selected-match window is incomplete; regenerate the report')
            ids_by_team[team['team_id']] = [int(fid) for fid in ids]
        ids = sorted({fid for values in ids_by_team.values() for fid in values})
        if len(ids) > 20:
            raise ValueError('The saved selected-match window is too large')
        with ThreadPoolExecutor(max_workers=min(4,len(ids)) or 1) as pool:
            raw = dict(zip(ids,pool.map(lambda fid: _fixture_lineup_usage(fid,0),ids)))
        for fid, row in raw.items():
            if int(row.get('id') or 0) != fid:
                raise ValueError('Selected match identity mismatch')
        league, season = int(fixture['league']['id']), int(fixture['season']['id'])
        cutoff = datetime.now(timezone.utc)
        for team in missing:
            selected = [raw[fid] for fid in ids_by_team[team['team_id']]]
            if any(kickoff(row.get('starting_at')) >= cutoff for row in selected):
                raise ValueError('Selected matches must already have been played')
            roster = {int(player['player_id']) for player in team.get('players') or []}
            sample = _team_recent_squad_usage({'id':team['team_id']},season,fixture['season'].get('name') or '',league,str(cutoff),
                _comparison_fixtures=selected,_comparison_player_ids=roster) if selected else {}
            data['teams'] = [group for group in data['teams'] if group['team_id'] != team['team_id']]
            data['teams'].append({'team_id':team['team_id'],'sample_size':len(selected),'status':'ready',
                'selected_fixture_ids':[row['id'] for row in selected],
                'players':[comparison_player(player) for player in sample.get('comparison_players') or []]})
        result = enrich_player_comparison({**usage,'player_comparison':data},fixture)['player_comparison']
        future.set_result(deepcopy(result))
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _PENDING_LOCK:
            _PENDING.pop(key,None)


PLAYER_OVERVIEW_VERSION = 2


def player_comparison_overview_evidence(usage, teams, lang):
    """Anonymous role distributions across the roster, independent of selectors."""
    from statistics import median
    from report_module.metric_validation import valid_metric

    data = usage.get('player_comparison') or {}
    result = []
    for team in sorted(teams, key=lambda item: 0 if item.get('location') == 'home' else 1)[:2]:
        group = next((item for item in data.get('teams') or [] if int(item['team_id']) == int(team['id'])), {})
        players = group.get('players') or []
        if not players:
            old = next((item for item in usage.get('teams') or [] if int(item['team_id']) == int(team['id'])), {})
            players = [comparison_player(player) for player in old.get('comparison_players') or []]
        roles = {}
        seen = set()
        for player in players:
            pid = player.get('player_id')
            if pid in seen or (numeric(player.get('minutes')) or 0) < 90:
                continue
            seen.add(pid)
            position = player.get('position_id')
            role = {24: 'goalkeeper', 25: 'defender', 26: 'midfielder', 27: 'attacker'}.get(position, 'other')
            state = roles.setdefault(role, {'role': role, 'player_count': 0, 'metrics': {}})
            state['player_count'] += 1
            for metric in {m['name']: m for m in player.get('metrics') or []}.values():
                name = metric['name']
                if not valid_metric(name, metric.get('value'), nonzero=True):
                    continue
                entry = state['metrics'].setdefault(name, {'values': [], 'category': metric.get('category') or metric_category(name),
                    'lower_is_better': metric.get('lower_is_better', name in LOWER_BETTER or metric_category(name) == 'errors_discipline'),
                    'is_percentage': bool(metric.get('is_percentage') or '%' in name)})
                entry['values'].append(numeric(metric['value']))
        role_groups = []
        for state in roles.values():
            metrics = [{'name': name, 'category': metric['category'], 'lower_is_better': metric['lower_is_better'],
                        'basis': 'percentage' if metric['is_percentage'] else 'per90',
                        'players_covered': len(metric['values']), 'median_value': round(median(metric['values']), 4)}
                       for name, metric in sorted(state['metrics'].items())]
            role_groups.append({**state, 'metrics': metrics})
        result.append({'team_name': team.get('name'), 'player_count': len(seen), 'roles': role_groups})
    return {'scope': 'whole_player_pool_by_role', 'stats_basis': 'selected_matches_per90',
            'aggregation': 'median of recorded nonzero player values within each role; not team totals',
            'teams': result}


def player_comparison_overview(usage, teams, lang):
    """General role-level fallback; never a comparison of two default players."""
    tr = lang == 'tr'
    evidence = player_comparison_overview_evidence(usage, teams, lang)
    pools = evidence['teams']
    names = [str(pool.get('team_name') or ('Ev sahibi' if index == 0 else 'Deplasman')) for index, pool in enumerate(pools)]
    names = (names + (['Ev sahibi', 'Deplasman'] if tr else ['Home', 'Away']))[:2]
    summary = (f"{names[0]} ile {names[1]} oyuncu gruplarının seçili maçlardaki yaratıcılık, bitiricilik ve savunma katkıları birlikte değerlendiriliyor. Mevki bazındaki ortak göstergeler, bireysel üretimin takım geneline nasıl yayıldığını ve yaklaşan maçın farklı alanlarında oluşabilecek katkı dengesini anlamaya yardımcı oluyor."
               if tr else f"The player groups of {names[0]} and {names[1]} are assessed through creativity, finishing and defensive contributions across the selected matches. Shared role-level indicators provide context for how individual output may shape the upcoming matchup.")
    labels = {'Key Passes': ('yaratıcılık', 'creativity'), 'Shots On Target': ('isabetli şut üretimi', 'shots on target'),
              'Successful Dribbles': ('bire bir hücum katkısı', 'one-on-one attacking output'),
              'Tackles': ('top kapma katkısı', 'tackling output'), 'Possession Lost': ('top kaybını sınırlama', 'limiting possession losses')}
    candidates = []
    if len(pools) == 2:
        by_role = [{role['role']: role for role in pool['roles']} for pool in pools]
        for role in sorted(by_role[0].keys() & by_role[1].keys()):
            metrics = [{m['name']: m for m in roles[role]['metrics']} for roles in by_role]
            for name in labels.keys() & metrics[0].keys() & metrics[1].keys():
                a, b = (values[name]['median_value'] for values in metrics)
                if min(values[name]['players_covered'] for values in metrics) < 2:
                    continue
                gap = abs(a - b) / max(abs(a), abs(b))
                if gap > 0.10:
                    winner = 0 if (a < b if metrics[0][name]['lower_is_better'] else a > b) else 1
                    candidates.append((gap, name, role, winner))
    if candidates:
        _, name, role, winner = max(candidates)
        role_tr = {'defender': 'savunma', 'midfielder': 'orta saha', 'attacker': 'hücum', 'goalkeeper': 'kaleci', 'other': 'diğer'}[role]
        summary = (f"{names[winner]} {role_tr} oyuncularının {labels[name][0]} göstergesinde öne çıkması, yaklaşan maçın bu alanında katkı dengesini etkileyebilir. {names[1-winner]} karşısında bu katkının sürekliliği, hücumların gelişimi ile savunma sonrası toparlanma arasındaki denge açısından takip edilecek noktalardan biri olabilir."
                   if tr else f"The {role} group of {names[winner]} shows stronger {labels[name][1]} across the selected matches, potentially influencing this area against {names[1-winner]}. This reflects recorded output across comparable roles on a common playing-time basis.")
    return {'category': 'Oyuncu Karşılaştırması' if tr else 'Player Comparison', 'summary': summary,
            'sub_bullets': [], 'player_overview_version': PLAYER_OVERVIEW_VERSION}


def localize_overview_text(text, lang):
    if lang != 'tr':
        return str(text or '')
    return re.sub(r'\bper[\s‐‑–—-]*90(?:\s+(?:minutes?|mins?))?\b', '90 dakika başına', str(text or ''), flags=re.I)


def player_overview_mentions_player(summary, usage):
    pools = [group.get('players') or [] for group in (usage.get('player_comparison') or {}).get('teams') or []]
    pools.extend(team.get('comparison_players') or [] for team in usage.get('teams') or [])
    names = {str(player.get('player_name') or '').strip() for pool in pools for player in pool}
    return any(len(name) >= 4 and re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', summary, re.I) for name in names)


def ensure_player_comparison_overview(usage, teams, lang):
    if 'overview_summary' not in usage:
        return usage
    rows = [{**row, 'summary': localize_overview_text(row.get('summary'), lang),
             'sub_bullets': [{**bullet, 'label': localize_overview_text(bullet.get('label'), lang),
                             'text': localize_overview_text(bullet.get('text'), lang)}
                            for bullet in row.get('sub_bullets') or []]}
            for row in usage.get('overview_summary') or []]
    labels = {'Oyuncu Karşılaştırması', 'Player Comparison'}
    existing = next((row for row in rows if row.get('category') in labels), None)
    row = existing if existing and existing.get('player_overview_version') == PLAYER_OVERVIEW_VERSION else player_comparison_overview(usage, teams, lang)
    rows = [item for item in rows if item.get('category') not in labels]
    index = next((i + 1 for i, item in enumerate(rows) if item.get('category') in {'Takım Analizi', 'Team Analysis'}), len(rows))
    rows.insert(index, row)
    return {**usage, 'overview_summary': rows}
