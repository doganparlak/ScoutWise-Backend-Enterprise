"""Competition-specific evidence for the independently sampled pre-match teams."""
import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api_module.database import benchmark_engine
from standings_module.metrics import normalize_fixture
from standings_module.team_derived import build_team_derived

VERSION = 1
_LOG = logging.getLogger(__name__)


def finding_snapshots(fixtures, full_fixtures):
    """Reuse the fixture detail responses collected for the squad report."""
    details = {row.get('id'): row for row in full_fixtures if row.get('id')}
    rows = {}
    for fixture in fixtures:
        raw = details.get(fixture.get('id'))
        if not raw:
            continue
        try:
            contribution = normalize_fixture({**fixture, **raw, 'lineups': []})
        except (ValueError, TypeError, KeyError):
            continue
        rows[fixture['id']] = {**contribution, 'league': fixture.get('league'),
                               'season': fixture.get('season'), 'stage': raw.get('stage')}
    return rows


def _stored_snapshots(usages):
    ids = {value for usage in usages for value in usage.get('selected_fixture_ids', [])}
    if not ids:
        return {}
    try:
        with benchmark_engine.connect() as db:
            rows = db.execute(text("""
                SELECT fixture_id, league_id, season_id, kickoff_at, team_contributions,
                       coverage->>'scope' AS phase
                FROM league_match_contributions
                WHERE fixture_id=ANY(CAST(:ids AS bigint[])) AND included_in_aggregation=TRUE
            """), {'ids': list(ids)}).mappings()
            return {row['fixture_id']: dict(row) for row in rows}
    except (SQLAlchemyError, OSError):
        _LOG.warning('Saved pre-match competition inputs unavailable')
        return {}


def build_scoped_findings(usages, lang, fixture, snapshots=None):
    from match_report_module.team_profile_comparison import (
        _attach_competition_matches, _competition_badge, _competition_scopes, _finding_metrics,
        _finding_options, _paired_performance,
    )

    rows = snapshots if snapshots is not None else _stored_snapshots(usages)
    output = {}
    for usage in usages:
        team_id = int(usage['team_id'])
        reports = []
        results = {row['fixture_id']: row for row in usage.get('results', []) if row.get('fixture_id')}
        ids = usage.get('selected_fixture_ids') or list(results)
        # Saved reports can be missing contributions. Preserve the original
        # sample rather than silently shortening the selected ten.
        if not ids or any(fid not in rows for fid in ids):
            continue
        for fid in ids:
            row = rows[fid]
            result = results.get(fid, {})
            league = row.get('league') or {'id': row['league_id'], 'name': result.get('league_name')}
            if league['id'] == (fixture.get('league') or {}).get('id'):
                league = {**fixture['league'], **league}
            stage = row.get('stage') or ({'type_id': 225} if row.get('phase') == 'qualifying' else {})
            derived = build_team_derived([row], include_profile_rates=True)['teams']
            teams = [{'id': int(tid), 'categories': {'recorded': [
                {'name': name, 'value': value} for name, value in {
                    **(data.get('metrics') or {}),
                    **{name: metric['perMatch'] for name, metric in derived.get(str(tid), {}).items()},
                }.items()
            ]}} for tid, data in (row['team_contributions'] or {}).items()]
            reports.append({'fixture': {'id': fid, 'starting_at': str(row['kickoff_at'])},
                            'league': league, 'season': row.get('season') or {'id': row['season_id']},
                            'stage': stage, 'teams': teams})
        scopes = []
        for (league, season, phase), selected in _competition_scopes(reports).items():
            own, opponent = _paired_performance(selected, team_id)
            metrics = _finding_metrics(own, opponent)
            _attach_competition_matches(selected, team_id, ({'themes': [{'metrics': list(metrics.values())}]},))
            metrics = {name: metric for name, metric in metrics.items() if metric.get('competitionMatches')}
            strength, weakness = _finding_options(own, opponent, lang, metrics)
            scopes.append({'competition': _competition_badge(league, season, phase, selected),
                           'strength': strength['themes'], 'weakness': weakness['themes']})
        if scopes:
            preferred = (fixture.get('league') or {}).get('id')
            output[str(team_id)] = sorted(scopes, key=lambda row: (
                row['competition']['id'] != preferred, -row['competition']['matchCount']))
    return output


def plan_findings(scoped):
    """Provide the default competition's findings to the fixture-wide plan."""
    return {team: {side: [
        {'id': card['findingId'], 'title': card['title'], 'description': card['description'],
         'competition': scopes[0]['competition']['name'], 'metrics': [
             {'metric_name': metric['sourceKey'], 'value': metric['competitionMatches'][0]['value'],
              'percentile': metric['competitionMatches'][0]['percentile'],
              'context_only': metric.get('detail') == 'context'} for metric in card['metrics']
         ]} for card in scopes[0][side]
    ] for side in ('strength', 'weakness')} for team, scopes in scoped.items() if scopes}
