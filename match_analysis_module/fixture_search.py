"""Season-wide fixture discovery with bounded provider date windows."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import os

import requests
from sqlalchemy import text

from .match_analysis import SPORTMONKS_BASE_URL, MAX_DATE_RANGE_DAYS, SportMonksError, _fixture_out


def fixture_choices(db):
    leagues = db.execute(text("""SELECT DISTINCT league_id AS id, league_name AS name,
        COALESCE(league_country_name, '') AS country FROM player_comp_data
        WHERE league_id IS NOT NULL AND COALESCE(league_name, '') <> ''
        ORDER BY country,name,id""")).mappings().all()
    memberships = db.execute(text("""SELECT team_id,league_id,MAX(team_name) AS name,COUNT(*) AS coverage
        FROM player_comp_data WHERE team_id IS NOT NULL AND league_id IS NOT NULL
        AND COALESCE(team_name,'') <> '' GROUP BY team_id,league_id
        ORDER BY coverage DESC,league_id""")).mappings().all()
    teams = {}
    for row in memberships:
        team = teams.setdefault(row['team_id'], {'id':row['team_id'],'name':row['name'],
                                                'leagueId':row['league_id'],'leagueIds':[]})
        team['leagueIds'].append(row['league_id'])
    # Use current domestic membership for the season anchor when available.
    from .current_leagues import load_current_teams
    for current in load_current_teams(db):
        team = teams.get(current['team_id'])
        if team and current['league_id']:
            team['leagueId'] = current['league_id']
            if current['league_id'] not in team['leagueIds']:
                team['leagueIds'].append(current['league_id'])
    return {'leagueChoices':[dict(row) for row in leagues],
            'teamChoices':sorted(teams.values(),key=lambda row:(row['name'],row['id'])),
            'countries':sorted({row['country'] for row in leagues if row['country']}),
            'leagues':sorted({row['name'] for row in leagues}),
            'teams':sorted({team['name'] for team in teams.values()})}


def _fetch(path, **params):
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        raise SportMonksError('ScoutWise data service is not configured')
    try:
        response = requests.get(f'{SPORTMONKS_BASE_URL}/{path}',
                                params={'api_token':token,**params},timeout=45)
        if response.status_code != 200:
            raise SportMonksError(f'Fixture data request failed (HTTP {response.status_code})')
        return response.json()
    except (requests.RequestException, ValueError):
        raise SportMonksError('Fixture data service is temporarily unavailable') from None


def _season_dates(league_id):
    league = _fetch(f'leagues/{league_id}',include='currentSeason;seasons').get('data') or {}
    season = league.get('currentseason') or league.get('currentSeason')
    if not season:
        candidates = [row for row in league.get('seasons') or []
                      if row.get('starting_at') and row['starting_at'][:10] <= date.today().isoformat()]
        season = max(candidates,key=lambda row:row['starting_at'],default={})
    try:
        start,end = date.fromisoformat(season['starting_at'][:10]),date.fromisoformat(season['ending_at'][:10])
    except (TypeError,KeyError,ValueError):
        raise SportMonksError('Current season dates are unavailable') from None
    if end < start or (end-start).days > 730:
        raise SportMonksError('Current season dates are invalid')
    return start,end


def search_season_fixtures(db, filters):
    choices = fixture_choices(db)
    covered = {row['id'] for row in choices['leagueChoices']}
    league_id,team_id = filters.get('leagueId'),filters.get('teamId')
    if not league_id and not team_id:
        raise ValueError('Select a league or team')
    if league_id and league_id not in covered:
        raise ValueError('Select an available league')
    team = next((row for row in choices['teamChoices'] if row['id']==team_id),None)
    if team_id and not team:
        raise ValueError('Select an available team')
    if team and league_id and league_id not in team['leagueIds']:
        raise ValueError('Select a team from the selected league')
    start_value,end_value = filters.get('startDate'),filters.get('endDate')
    if not start_value or not end_value:
        season_start,season_end = _season_dates(league_id or team['leagueId'])
    start = date.fromisoformat(start_value) if start_value else season_start
    end = date.fromisoformat(end_value) if end_value else season_end
    if end < start:
        raise ValueError('endDate cannot be earlier than startDate')
    windows = []
    cursor = start
    while cursor <= end:
        stop = min(cursor+timedelta(days=MAX_DATE_RANGE_DAYS),end)
        windows.append((cursor,stop));cursor = stop+timedelta(days=1)
    def window_rows(window):
        first,last = window
        path = f'fixtures/between/{first}/{last}' + (f'/{team_id}' if team_id else '')
        rows,page = [],1
        while True:
            payload = _fetch(path,include='participants;league.country;season;stage;round;venue;state;referees;scores',
                             filters=f'fixtureLeagues:{league_id}' if league_id else None,
                             order='asc',per_page=50,page=page)
            for raw in payload.get('data') or []:
                item = _fixture_out(raw)
                if not item or item['league'].get('id') not in covered:
                    continue
                if league_id and item['league'].get('id') != league_id:
                    continue
                if team_id and team_id not in (item['homeTeam']['id'],item['awayTeam']['id']):
                    continue
                if not start.isoformat() <= item['startingAt'][:10] <= end.isoformat():
                    continue
                rows.append(item)
            pagination = payload.get('pagination') or (payload.get('meta') or {}).get('pagination') or {}
            if not pagination.get('has_more'):
                return rows,page
            page += 1
            if page > 100:
                raise SportMonksError('Fixture pagination exceeded the supported range')
    with ThreadPoolExecutor(max_workers=min(4,len(windows))) as executor:
        results = list(executor.map(window_rows,windows))
    fixtures = {row['fixtureId']:row for rows,_ in results for row in rows}
    return {'fixtures':sorted(fixtures.values(),key=lambda row:(row['startingAt'],row['fixtureId'])),
            'pagination':{'page':1,'hasMore':False,'pagesRead':sum(pages for _,pages in results)}}
