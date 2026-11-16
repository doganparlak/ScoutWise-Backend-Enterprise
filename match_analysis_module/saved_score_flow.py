"""Upgrade score-flow JSON in saved reports without regenerating other sections."""
import copy
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def needs_distinct_counts(states, snake=False):
    fields = ('matches_to_ahead', 'matches_to_level', 'matches_to_behind') if snake else ('matchesToAhead', 'matchesToLevel', 'matchesToBehind')
    return bool(states) and any(any(field not in row for field in fields) for row in states)


def upgrade_pre_match(content):
    usages = (content.get('recent_squad_usage') or {}).get('teams') or []
    missing = [usage for usage in usages if needs_distinct_counts((usage.get('score_flow') or {}).get('states') or [], True)]
    if not missing:
        return content
    # Freeze the original selection; never substitute today's recent fixtures.
    if any(not usage.get('selected_fixture_ids') for usage in missing):
        return content
    from .pre_match_report import _aggregate_score_flow, _fixture_lineup_usage
    from .score_flow_rates import rate_perspectives
    ids = sorted({int(fid) for usage in missing for fid in usage['selected_fixture_ids']})
    team_ids = {int(fid): int(usage['team_id']) for usage in missing for fid in usage['selected_fixture_ids']}
    try:
        with ThreadPoolExecutor(max_workers=min(5, len(ids))) as pool:
            fixtures = dict(zip(ids, pool.map(lambda fid: _fixture_lineup_usage(fid, team_ids[fid], include="participants;events.type"), ids)))
        result = copy.deepcopy(content)
        for usage in result['recent_squad_usage']['teams']:
            if usage not in missing:
                continue
            selected = [fixtures[int(fid)] for fid in usage['selected_fixture_ids']]
            if any(int(f.get('id') or 0) != int(fid) or not any(int(p.get('id') or 0) == int(usage['team_id']) for p in f.get('participants') or [])
                   for f, fid in zip(selected, usage['selected_fixture_ids'])):
                return content
            usage['score_flow'] = _aggregate_score_flow(selected, int(usage['team_id']))
        perspectives = result['recent_squad_usage'].setdefault('score_flow_perspectives', {})
        for usage in result['recent_squad_usage']['teams']:
            for state, narrative in rate_perspectives((usage.get('score_flow') or {}).get('states') or [], result.get('language') or 'en', snake=True).items():
                perspectives.setdefault(state, {})[str(usage['team_id'])] = narrative
        return result
    except Exception:
        logger.exception('Could not upgrade saved pre-match score flow; retaining the original report')
        return content


def upgrade_team(content, team_id, lang):
    if not needs_distinct_counts((content.get('scoreFlowProfile') or {}).get('states') or []) or not content.get('reports'):
        return content
    from match_report_module.report import build_team_report_score_flow_profile
    result = dict(content)
    result['scoreFlowProfile'] = build_team_report_score_flow_profile(content['reports'], team_id, lang, include_ai=False)
    return result
