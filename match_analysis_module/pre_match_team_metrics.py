"""Enterprise pre-match additions over the exact selected paired fixtures."""
from datetime import datetime, timezone

from sqlalchemy import text

from api_module.database import engine
from standings_module.metrics import TEAM_ADVANCED_METRICS, numeric
from standings_module.team_derived import DEFINITIONS, build_team_derived


def extend_comparison(categories, rows, team_id):
    """Share league definitions, retaining the report's penalty-xG semantics."""
    # Consume one-shot fixture streams once; retain only advanced totals.
    names = (TEAM_ADVANCED_METRICS | {'Penalties'}) - set(DEFINITIONS) - {'Expected Goals Penalties (pxG)'}
    advanced = {}
    def observed_rows():
        for row in rows:
            metrics = row.get('team_contributions', {}).get(str(team_id), {}).get('metrics') or {}
            for name in names:
                value = numeric(metrics.get(name))
                if value is not None:
                    total = advanced.setdefault(name, [0., 0])
                    total[0] += value
                    total[1] += 1
            yield row
    derived = build_team_derived(observed_rows())
    additions = {}
    for name, metric in derived['teams'].get(str(team_id), {}).items():
        _, category, preference = DEFINITIONS[name]
        additions[name] = (category, {
            'name': name, 'value': metric['perMatch'], 'total': metric['value'],
            'aggregation': 'derived_rate' if metric['aggregation'] == 'derived_rate' else 'per_match',
            'matches_covered': metric['matchesCovered'], 'preference': preference,
        })
    for name, (total, covered) in advanced.items():
        additions[name] = ('set_pieces' if name == 'Penalties' else 'expected', {
            'name': name, 'value': round(total / covered, 4),
            'total': round(total, 4), 'aggregation': 'per_match',
            'matches_covered': covered, 'preference': 'higher',
        })
    # Reopening an old snapshot must not retain stale ranks for replaced values.
    result = {group: [dict(metric) for metric in metrics if metric['name'] not in additions]
              for group, metrics in categories.items()}
    for group, metric in additions.values():
        result.setdefault(group, []).append(metric)
    return {group: sorted(metrics, key=lambda item: item['name']) for group, metrics in result.items() if metrics}


def enrich_selected_team_metrics(usage, fixture):
    """Upgrade saved reports from cached matches, without provider calls on open.

    Require every saved selected fixture; an incomplete cache must never silently
    change a ten-match comparison into a nine-match comparison.
    """
    windows = {}
    for team in usage.get('teams') or []:
        ids = team.get('selected_fixture_ids') or [r.get('fixture_id') for r in team.get('results') or []]
        ids = list(dict.fromkeys(int(fid) for fid in ids if fid))
        if ids and len(ids) == team.get('sample_size'):
            windows[int(team['team_id'])] = ids
    if not windows:
        return usage
    cutoff = datetime.now(timezone.utc)
    ids = list({fid for window in windows.values() for fid in window})
    with engine.connect() as db:
        rows = db.execute(text('''SELECT fixture_id,team_contributions FROM league_match_contributions
            WHERE fixture_id=ANY(CAST(:ids AS bigint[])) AND included_in_aggregation=TRUE
            AND kickoff_at<:cutoff'''), {'ids': ids, 'cutoff': cutoff}).mappings().all()
    by_id = {int(row['fixture_id']): row for row in rows}
    teams = []
    for team in usage.get('teams') or []:
        selected = windows.get(int(team['team_id']), [])
        complete = selected and all(fid in by_id and str(team['team_id']) in by_id[fid]['team_contributions'] for fid in selected)
        teams.append({**team, 'team_comparison': extend_comparison(
            team.get('team_comparison') or {}, [by_id[fid] for fid in selected], team['team_id'])} if complete else team)
    return {**usage, 'teams': teams}
