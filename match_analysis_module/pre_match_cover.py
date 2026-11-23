"""Fill missing pre-match coach names and portraits from team details."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy


def enrich_pre_match_coaches(content):
    from .match_analysis import _get_team_details, SportMonksError
    from requests import RequestException
    missing = [team for team in content.get('teams') or []
               if team.get('id') and (not str(team.get('coach_name') or '').strip() or not team.get('coach_image_url'))]
    if not missing:
        return content
    def lookup(team):
        try:
            detail = _get_team_details(int(team['id']))
            name = str(detail.get('coachName') or '').strip()
            saved_name = str(team.get('coach_name') or '').strip()
            if not name or (saved_name and saved_name.casefold() != name.casefold()):
                return team['id'], {}
            fields = {}
            if not saved_name:
                fields['coach_name'] = name
            if not team.get('coach_image_url') and detail.get('coachImageUrl'):
                fields['coach_image_url'] = detail['coachImageUrl']
            return team['id'], fields
        except (SportMonksError, RequestException, ValueError):
            return team['id'], {}
    with ThreadPoolExecutor(max_workers=min(2,len(missing))) as pool:
        updates = dict(pool.map(lookup, missing))
    if not any(updates.values()):
        return content
    result = deepcopy(content)
    for team in result['teams']:
        team.update(updates.get(team['id'], {}))
    return result
