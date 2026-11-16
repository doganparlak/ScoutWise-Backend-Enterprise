"""Fill missing pre-match coach names from the existing team-detail source."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy


def enrich_pre_match_coaches(content):
    from .match_analysis import _get_team_details, SportMonksError
    from requests import RequestException
    missing = [team for team in content.get('teams') or [] if team.get('id') and not str(team.get('coach_name') or '').strip()]
    if not missing:
        return content
    def lookup(team):
        try:
            return team['id'], str(_get_team_details(int(team['id'])).get('coachName') or '').strip()
        except (SportMonksError, RequestException, ValueError):
            return team['id'], ''
    with ThreadPoolExecutor(max_workers=min(2,len(missing))) as pool:
        names = {key:name for key,name in pool.map(lookup,missing) if name}
    if not names:
        return content
    result = deepcopy(content)
    for team in result['teams']:
        if team['id'] in names:
            team['coach_name'] = names[team['id']]
    return result
