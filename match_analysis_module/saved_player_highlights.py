"""Validate saved highlight cards and replace unsupported development selections."""
from copy import deepcopy


def upgrade_player_highlights(content):
    from .pre_match_report import (
        _DEVELOPMENT_PLAYER_MIN_MINUTES, _attach_player_highlight_metrics,
        _can_interpret_player, _distinct_player_highlight_metrics,
    )
    usages = (content.get('recent_squad_usage') or {}).get('teams') or []
    changes = []
    comparison = (content.get('recent_squad_usage') or {}).get('player_comparison') or {}
    for index, usage in enumerate(usages):
        summary = usage.get('performance_summary') or {}
        sample_size = int(usage.get('sample_size') or 0)
        reselected = None
        for field in ('featured_players', 'top_scorers', 'top_rated', 'top_scorer', 'development'):
            value = summary.get(field)
            players = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
            selection = 'development' if field == 'development' else 'featured'
            metric_field = 'development_metrics' if field == 'development' else 'standout_metrics'
            valid = []
            for player in players:
                metrics = player.get(metric_field) or []
                distinct = _distinct_player_highlight_metrics(metrics)
                if len(distinct) < len(metrics):
                    if reselected is None:
                        selected = usage.get('selected_fixture_ids') or [r.get('fixture_id') for r in usage.get('results') or []]
                        source = next((row for row in comparison.get('teams') or [] if row.get('team_id') == usage.get('team_id')), {})
                        same_window = (comparison.get('stats_scope') == 'selected_matches' and selected
                                       and set(selected) == set(source.get('selected_fixture_ids') or [])
                                       and source.get('sample_size') == sample_size)
                        candidates = [{**p, 'per90_metrics': p.get('metrics') or []} for p in source.get('players') or []] if same_window else []
                        _attach_player_highlight_metrics(candidates, sample_size)
                        reselected = {p['player_id']: p for p in candidates}
                    alternate = reselected.get(player['player_id'], {}).get(metric_field) or []
                    player = {**player, metric_field: _distinct_player_highlight_metrics([*distinct, *alternate])}
                # A saved card may have two sound metrics after deduplication.
                if _can_interpret_player(player, selection, sample_size):
                    valid.append(player)
            if valid != players:
                changes.append((index, field, valid if isinstance(value, list) else next(iter(valid), None)))
    result = deepcopy(content)
    usage = result['recent_squad_usage']
    removed = set()
    for index, field, value in changes:
        old = usage['teams'][index]['performance_summary'][field]
        for p in old if isinstance(old, list) else [old]:
            removed.add(str(p.get('player_id') or p.get('player_name')))
        usage['teams'][index]['performance_summary'][field] = value
    retained = set()
    for team in usage['teams']:
        summary = team.get('performance_summary') or {}
        for field in ('featured_players', 'top_scorers', 'top_rated', 'top_scorer', 'development'):
            value = summary.get(field)
            for p in value if isinstance(value, list) else [value] if isinstance(value, dict) else []:
                retained.add(str(p.get('player_id') or p.get('player_name')))
    # Comparison metrics are usable only when they describe exactly the same
    # frozen fixtures as the report; never substitute today's recent matches.
    replaced = False
    comparison = usage.get('player_comparison') or {}
    if (content.get('pre_match_schema_version') or 0) < 15 and comparison.get('stats_scope') == 'selected_matches':
        for team in usage['teams']:
            summary = team.get('performance_summary') or {}
            if summary.get('development'):
                continue
            source = next((t for t in comparison.get('teams') or [] if t.get('team_id') == team.get('team_id')), {})
            selected = team.get('selected_fixture_ids') or [r.get('fixture_id') for r in team.get('results') or []]
            if not selected or set(selected) != set(source.get('selected_fixture_ids') or []) or source.get('sample_size') != team.get('sample_size'):
                continue
            players = [{**p, 'per90_metrics':deepcopy(p.get('metrics') or [])} for p in source.get('players') or []
                       if (p.get('minutes') or 0)>90 and isinstance(p.get('average_rating'),(int,float))]
            _attach_player_highlight_metrics(players,team['sample_size'])
            candidates = [p for p in players if p['minutes'] >= _DEVELOPMENT_PLAYER_MIN_MINUTES
                          and str(p.get('position_name') or '').strip().casefold() not in {'goalkeeper','kaleci'}
                          and _can_interpret_player(p,'development',team['sample_size'])]
            if not candidates:
                continue
            player = min(candidates,key=lambda p:(p['average_rating'],-p['minutes'],p['player_name']))
            team.setdefault('performance_summary',{})['development'] = player
            retained.add(str(player.get('player_id') or player.get('player_name')))
            replaced = True
    if not changes and not replaced:
        return content
    for key in removed - retained:
        (usage.get('player_perspectives') or {}).pop(key, None)
    return result
