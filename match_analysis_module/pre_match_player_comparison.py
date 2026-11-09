"""Enterprise player comparisons over the report's exact selected fixtures."""
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from datetime import datetime, timezone
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


def _overview_players(usage, teams, lang):
    """Use the same highest-minute midfielder defaults as the comparison tab."""
    tr = lang == 'tr'
    ordered = sorted(teams, key=lambda team: 0 if team.get('location') == 'home' else 1)
    names = [str(team.get('name') or ('Ev sahibi' if tr else 'Home')) for team in ordered[:2]]
    if len(names) < 2:
        names = ['Ev sahibi', 'Deplasman'] if tr else ['Home', 'Away']
    selected = []
    for team in ordered[:2]:
        group = next((item for item in (usage.get('player_comparison') or {}).get('teams') or []
                      if int(item['team_id']) == int(team['id'])), {})
        players = group.get('players') or []
        if not players:
            old = next((item for item in usage.get('teams') or [] if int(item['team_id']) == int(team['id'])), {})
            players = [comparison_player(player) for player in (old.get('performance_summary') or {}).get('featured_players') or []]
        def midfielder(player):
            return player.get('position_id') == 26 or any(word in str(player.get('position_name') or '').lower() for word in ('midfielder', 'orta saha'))
        outfield = [player for player in players if player.get('position_id') != 24 and not any(word in str(player.get('position_name') or '').lower() for word in ('goalkeeper', 'kaleci'))]
        candidates = [player for player in players if midfielder(player)] or outfield or players
        selected.append(max(candidates, key=lambda player: player.get('minutes') or 0, default={}))
    return ordered, names, selected


def player_comparison_overview_evidence(usage, teams, lang):
    ordered, names, selected = _overview_players(usage, teams, lang)
    data = usage.get('player_comparison') or {}
    supported = {'Key Passes', 'Shots On Target', 'Successful Dribbles', 'Assists', 'Goals', 'Tackles', 'Yellow Cards', 'Possession Lost'}
    return {'competition_name': data.get('competition_name'), 'season_name': data.get('season_name'),
            'stats_basis': 'selected_matches_per90', 'ranking_basis': 'competition_season_per90',
            'players': [{**{key: player.get(key) for key in ('player_id', 'player_name', 'position_name', 'minutes')},
                         'team_name': names[index],
                         'metrics': [metric for metric in player.get('metrics') or [] if metric['name'] in supported and (numeric(metric.get('value')) or 0) > 0]}
                        for index, player in enumerate(selected)]}


def player_comparison_overview(usage, teams, lang):
    """A compact evidence-led fallback, also usable by older saved reports."""
    tr = lang == 'tr'
    _, names, selected = _overview_players(usage, teams, lang)
    summary = (f"{names[0]} ile {names[1]} oyuncularının seçili maçlardaki katkıları ortak süre temelinde karşılaştırılıyor. Bu yaklaşım, farklı dakika toplamlarını dengeleyerek bireysel üretimi daha anlaşılır kılıyor; takım düzeyindeki görünümün yanında oyuncuların rol katkılarını ve karşılaşmadaki bireysel eşleşmeleri birlikte değerlendirmeyi sağlıyor."
               if tr else f"Comparing players from {names[0]} and {names[1]} on a common playing-time basis clarifies individual output across the selected matches. This complements team-level analysis by distinguishing role contributions and providing context for individual matchups.")
    labels = [('Key Passes', 'kilit pas'), ('Shots On Target', 'isabetli şut'), ('Successful Dribbles', 'başarılı dripling'), ('Assists', 'asist'), ('Goals', 'gol'), ('Tackles', 'top kapma')]
    if len(selected) == 2:
        metrics = [{metric['name']: metric for metric in player.get('metrics') or []} for player in selected]
        for metric, label in labels:
            a, b = (numeric(values.get(metric, {}).get('value')) for values in metrics)
            if a is None or b is None or a <= 0 or b <= 0 or a == b:
                continue
            winner = names[0 if a > b else 1]
            summary = (f"{names[0]} ile {names[1]} oyuncuları seçili maçlar üzerinden karşılaştırıldığında, {label} üretiminde {winner} tarafı öne çıkıyor. Ortak süre temeli, oyuncuların farklı dakika toplamlarını dengeleyerek iki takımın bireysel katkı düzeyleri arasındaki farkı daha görünür kılıyor."
                       if tr else f"Comparing players from {names[0]} and {names[1]} across the selected matches highlights stronger {metric.lower()} output on the {winner} side. A common playing-time basis clarifies individual contribution differences despite unequal minutes played.")
            break
    return {'category': 'Oyuncu Karşılaştırması' if tr else 'Player Comparison', 'summary': summary, 'sub_bullets': []}


def ensure_player_comparison_overview(usage, teams, lang):
    if 'overview_summary' not in usage:
        return usage
    rows = list(usage.get('overview_summary') or [])
    labels = {'Oyuncu Karşılaştırması', 'Player Comparison'}
    existing = next((row for row in rows if row.get('category') in labels), None)
    row = existing or player_comparison_overview(usage, teams, lang)
    rows = [item for item in rows if item.get('category') not in labels]
    index = next((i + 1 for i, item in enumerate(rows) if item.get('category') in {'Takım Analizi', 'Team Analysis'}), len(rows))
    rows.insert(index, row)
    return {**usage, 'overview_summary': rows}
