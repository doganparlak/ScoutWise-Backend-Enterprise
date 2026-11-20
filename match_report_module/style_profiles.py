"""Single-match attacking/defensive profiles using the shared style catalogue."""
from bisect import bisect_left, bisect_right
from math import isfinite

from report_module.attacking_styles import select_team_styles

VERSION = 7
TASKS = {
    'attack': [('Goals',1.3,'finishing'),('Expected Goals',1,'finishing'),('Shots Total',1,'shots'),
               ('Key Passes',1.2,'creation'),('Expected Assists',1,'creation'),('Assists',1.1,'assists'),
               ('Successful Dribbles',1,'carrying'),('Passes In Final Third',.8,'progression'),
               ('Through Balls',.8,'progression'),('Accurate Crosses',.8,'width')],
    'defence': [('Tackles Won',1.2,'tackling'),('Tackles',1,'tackling'),('Interceptions',1.2,'interceptions'),
                ('Ball Recovery',1.2,'recoveries'),('Duels Won',1,'duels'),('Aerials Won',1,'aerials'),
                ('Clearances',.8,'clearances'),('Blocked Shots',1,'blocks')],
}
RATE_PAIRS = {'Shots Total':'Shots On Target (%)','Successful Dribbles':'Dribble Accuracy (%)',
              'Accurate Crosses':'Accurate Crosses (%)','Tackles Won':'Tackles Won (%)',
              'Duels Won':'Duels Won (%)','Aerials Won':'Aerials Won (%)'}


def _values(row):
    from coach_pool_module.profiles import METRIC_ALIASES
    result = {}
    for metric in [*[m for rows in (row.get('categories') or {}).values() for m in rows],*(row.get('expected_metrics') or [])]:
        if metric.get('_inferred_zero'):
            continue
        name = METRIC_ALIASES.get(metric.get('name'),metric.get('name'))
        value = metric.get('value')
        if value is None or isinstance(value,bool) or value == '':
            continue
        try:
            value = float(str(value).rstrip('%'))
        except (ValueError,TypeError):
            continue
        if name and isfinite(value) and value>=0 and ('%' not in name or value<=100):
            result[name] = value
    return result


def _cell(name,value):
    rate = '%' in name or 'percentage' in name.lower()
    shown = f'{value:.2f}'.rstrip('0').rstrip('.')
    return {'label':name,'value':('%' if rate else '')+shown,'basis':'rate' if rate else 'perMatch','contributes':True}


def select_players(report,team_id,side):
    players = []
    for row in report.get('lineups') or []:
        if int(row.get('team_id') or 0)!=int(team_id):
            continue
        position = str(row.get('position_name') or '').casefold()
        if row.get('position_id')==24 or 'goalkeeper' in position or 'kaleci' in position:
            continue
        values = _values(row)
        if values.get('Minutes Played',0)<=0:
            continue
        players.append((row,values))
    totals = {name:sum(values.get(name,0) for _,values in players) for name,_,_ in TASKS[side]}
    ranked = []
    for row,values in players:
        signals = [(name,weight*values[name]/totals[name],family) for name,weight,family in TASKS[side]
                   if values.get(name,0)>0 and totals[name]>0]
        signals.sort(key=lambda item:(-item[1],item[0]))
        families, chosen = set(), []
        for name,_,family in signals:
            if family not in families:
                chosen.append(name); families.add(family)
            if len(chosen)==4:
                break
        # A relevant success rate can complete a player's count/rate picture.
        if len(chosen)<4:
            chosen += [RATE_PAIRS[name] for name in chosen if name in RATE_PAIRS and RATE_PAIRS[name] in values][:4-len(chosen)]
        if len(chosen)<3:
            continue
        score = sum(max(s[1] for s in signals if s[2]==family) for family in families)
        source = {name:_cell(name,values[name]) for name in chosen}
        ranked.append((score,{'playerId':row['player_id'],'name':row.get('player_name') or '',
                             'imageUrl':row.get('player_image_url'),'position':row.get('position_name') or '',
                             'minutes':values['Minutes Played'],'averageRating':values.get('Rating',0),
                             'source_metrics':source}))
    ranked.sort(key=lambda item:(-item[0],-item[1]['minutes'],str(item[1]['playerId'])))
    return [player for _,player in ranked[:2]]


def _attach_percentiles(report, profiles):
    from sqlalchemy.exc import SQLAlchemyError
    from .style_profile_ranks import lineup_style_ranks
    from standings_module.comparison_ranks import canonical_metric_name, competition_ranks
    from standings_module.metrics import kickoff, numeric
    from standings_module.pre_match_player_ranks import cached_player_ranks
    from standings_module.stages import EUROPEAN_LEAGUES, stage_scope

    fixture = report.get('fixture') or {}
    league = report.get('league') or {}
    season = report.get('season') or {}
    league_id = int(league.get('id') or fixture.get('league_id') or 0)
    season_id = int(season.get('id') or fixture.get('season_id') or 0)
    if not league_id or not season_id or not fixture.get('starting_at'):
        return
    cutoff = kickoff(fixture['starting_at'])
    if not cutoff:
        return
    scope = ('qualifying' if league_id in EUROPEAN_LEAGUES
             and stage_scope(report.get('stage') or {}) == 'qualifying' else 'overall')
    cards = [(team['team_id'], kind, row)
             for team in profiles['teams'] for field in ('attackProfile', 'defenseProfile')
             for kind in ('players', 'themes') for row in team[field][kind]]
    targets = {(team_id, int(row['playerId'])) for team_id, kind, row in cards if kind == 'players'}
    player_distributions = {canonical_metric_name(metric.get('sourceKey') or metric['label']): None
                            for _, kind, row in cards if kind == 'players' for metric in row['metrics']}
    try:
        team_ranks = competition_ranks(league_id, season_id, cutoff, scope)
    except (SQLAlchemyError, OSError, RuntimeError, ValueError, TypeError, KeyError):
        team_ranks = {}
    try:
        player_ranks = cached_player_ranks(league_id, season_id, scope, int(cutoff.timestamp()) // 60,
                                           mode='perMatch', players=targets, distributions=player_distributions)
    except (SQLAlchemyError, OSError, RuntimeError, ValueError, TypeError, KeyError):
        player_ranks = {}

    if any(metric.get('sourceKey', '').startswith(('role:', 'Outfield '))
           for _, kind, row in cards if kind == 'themes' for metric in row['metrics']):
        try:
            lineup_ranks = lineup_style_ranks(league_id, season_id, cutoff, scope)
        except (SQLAlchemyError, OSError, RuntimeError, ValueError, TypeError, KeyError):
            lineup_ranks = {}
        team_ranks = {tid: {**team_ranks.get(tid, {}), **lineup_ranks.get(tid, {})}
                      for tid in team_ranks.keys() | lineup_ranks.keys()}

    team_distributions = {}
    for ranks in team_ranks.values():
        for name, rank in ranks.items():
            values, _ = team_distributions.setdefault(name, ([], rank['direction']))
            values.append(rank['season_value'])
    for values, _ in team_distributions.values():
        values.sort()

    profiles['competition'] = {'id': league_id, 'name': league.get('name') or '',
                               'imageUrl': league.get('image_path'), 'scope': scope}
    for team_id, kind, row in cards:
        player = kind == 'players'
        ranks = (player_ranks.get((team_id, int(row['playerId'])), {}) if player
                 else team_ranks.get(str(team_id), {}))
        distributions = player_distributions if player else team_distributions
        for metric in row['metrics']:
            name = canonical_metric_name(metric.get('sourceKey') or metric['label'])
            cohort = distributions.get(name)
            if not cohort or len(cohort[0]) < 5:
                continue
            values, direction = cohort
            total = len(values)
            rank = ranks.get(name) or {}
            position = rank.get('position')
            if position is not None and 1 <= position <= total:
                metric['percentile'] = round(100 * (total - position) / (total - 1))
                metric['benchmarkValue'] = rank.get('season_value')
            match_value = numeric(str(metric['value']).strip('%').replace(',', '.'))
            if match_value is not None:
                better = bisect_left(values, match_value) if direction == 'asc' else total - bisect_right(values, match_value)
                metric['matchPercentile'] = round(100 * max(0, total - 1 - better) / (total - 1))
            metric['benchmarkSize'] = total


def build_match_profiles(report, lang='en'):
    profiles = {'version': VERSION, 'teams': []}
    for team in report.get('teams') or []:
        team_id = int(team['id'])
        row = {'team_id': team_id}
        for side, field in (('attack', 'attackProfile'), ('defence', 'defenseProfile')):
            styles = select_team_styles([report], team_id, lang, side, single_match=True)
            row[field] = {
                'themes': [{**{key: style[key] for key in ('styleId', 'styleSide', 'title', 'description')},
                            'metrics': list(style['source_metrics'].values()), 'analysis': []} for style in styles],
                'players': [{**{key: value for key, value in player.items() if key != 'source_metrics'},
                             'metrics': list(player['source_metrics'].values()), 'analysis': []}
                            for player in select_players(report, team_id, side)],
            }
        profiles['teams'].append(row)
    _attach_percentiles(report, profiles)
    return profiles


def upgrade_match_profiles(content):
    if content.get('report_type')=='pre_match' or (content.get('match_profiles') or {}).get('version')==VERSION:
        return content
    try:
        return {**content,'match_profiles':build_match_profiles(content,content.get('language') or 'en')}
    except Exception as exc:
        print(f'[enterprise_match_report] event=match_profiles_deferred error_type={type(exc).__name__}')
        return content
