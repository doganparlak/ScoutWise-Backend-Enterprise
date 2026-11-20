"""Two evidence-backed findings per team from the actual match."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from itertools import chain, combinations, product
import logging

from sqlalchemy.exc import SQLAlchemyError

from match_analysis_module.pre_match_findings import TOPIC_ANCHORS, TOPIC_METRICS, _supports
from report_module.team_findings import CATALOG, number
from standings_module.comparison_ranks import canonical_metric_name, competition_ranks
from standings_module.metrics import kickoff
from standings_module.stages import EUROPEAN_LEAGUES, stage_scope
from standings_module.team_derived import MIRRORS

VERSION = 8
_LOG = logging.getLogger(__name__)


# A count and rates that reuse the same outcome cannot establish independent evidence.
_METRIC_FAMILIES = (
    {'Goals', 'Goal Conversion (%)', 'On Target Goal Conversion (%)'},
    {'Shots On Target', 'Shots On Target (%)'},
    {'Expected Goals', 'Expected Goals (xG)', 'Expected Goals Open Play (xGOP)', 'Expected Goals Non Penalty Goals (npxG)'},
    {'Shots Total', 'Shots Insidebox', 'Shots Outsidebox'},
    {'Passes', 'Accurate Passes', 'Accurate Passes (%)'},
    {'Total Crosses', 'Accurate Crosses', 'Accurate Crosses (%)', 'Unsuccessful Crosses'},
    {'Fouls', 'Fouls Per 100 Opponent Passes'},
    {'Ball Recovery', 'Recoveries Per 100 Opponent Passes'},
    {'Interceptions', 'Interceptions Per 100 Opponent Passes'},
    {'Total Cards', 'Yellow Cards', 'Red Cards'},
)
_FAMILY_BY_METRIC = {name: index for index, group in enumerate(_METRIC_FAMILIES) for name in group}


def _family(name):
    source = MIRRORS.get(name, (name.removeprefix('Opponent '),))[0]
    return _FAMILY_BY_METRIC.get(source, source)


def _match_metrics(team):
    return {
        canonical_metric_name(row['name']): {'value': value, 'matches_covered': 1}
        for rows in [*(team.get('categories') or {}).values(), team.get('expected_metrics') or []]
        for row in rows if row.get('name') and not row.get('_inferred_zero')
        and (value := number(row.get('value'))) is not None
    }


def _comparison_metrics(team, opponent):
    values = dict(team)
    for name, (source, _, _) in MIRRORS.items():
        if source in opponent and name not in values:
            values[name] = dict(opponent[source])
    for name, numerator, denominator in (
        ('Goal Conversion (%)', 'Goals', 'Shots Total'),
        ('On Target Goal Conversion (%)', 'Goals', 'Shots On Target'),
        ('Fouls Per 100 Opponent Passes', 'Fouls', 'Opponent Passes'),
        ('Recoveries Per 100 Opponent Passes', 'Ball Recovery', 'Opponent Passes'),
        ('Interceptions Per 100 Opponent Passes', 'Interceptions', 'Opponent Passes'),
    ):
        top, bottom = values.get(numerator), values.get(denominator)
        if name not in values and top and bottom and bottom['value'] > 0:
            ratio = top['value'] / bottom['value']
            if not name.endswith('(%)') or ratio <= 1:
                values[name] = {'value': 100 * ratio, 'matches_covered': 1}
    return values


def _attach_match_percentiles(content, samples):
    fixture = content.get('fixture') or {}
    league = (content.get('league') or {}).get('id') or fixture.get('league_id')
    season = (content.get('season') or {}).get('id') or fixture.get('season_id')
    if not league or not season or not fixture.get('starting_at'):
        return
    try:
        cutoff = kickoff(fixture['starting_at'])
        if not cutoff:
            return
        scope = ('qualifying' if int(league) in EUROPEAN_LEAGUES
                 and stage_scope(content.get('stage') or {}) == 'qualifying' else 'overall')
        ranks = competition_ranks(int(league), int(season), cutoff, scope)
    except (SQLAlchemyError, OSError, RuntimeError, ValueError, KeyError, TypeError):
        _LOG.warning('Match team finding league benchmarks unavailable for fixture %s', fixture.get('id'))
        return
    distributions = defaultdict(list)
    directions = {}
    for team_ranks in ranks.values():
        for name, rank in team_ranks.items():
            distributions[name].append(rank['season_value'])
            directions[name] = rank['direction']
    for values in distributions.values():
        values.sort()
    for team_id, sample in samples:
        team_ranks = ranks.get(str(team_id), {})
        for name, metric in sample.items():
            values = distributions[name]
            total = len(values)
            if total >= 5:
                value = metric['value']
                better = (bisect_left(values, value) if directions[name] == 'asc'
                          else total - bisect_right(values, value))
                match_position = min(total, better + 1)
                metric['percentile'] = round(100 * (total - match_position) / (total - 1))
                metric['match_position'] = match_position
                metric['total_teams'] = total
                metric['direction'] = directions[name]
                position = team_ranks.get(name, {}).get('position')
                if position is not None and 1 <= position <= total:
                    metric['season_position'] = position
                    metric['season_percentile'] = round(100 * (total - position) / (total - 1))


def _candidate_pairs(own, opponent):
    by_topic = defaultdict(list)
    for topic, pool in TOPIC_METRICS.items():
        common = {name for name in pool & own.keys() & opponent.keys()
                  if own[name]['value'] > 0 and opponent[name]['value'] > 0}
        if len(common) < 2:
            continue
        gaps = {name: abs(own[name]['value'] - opponent[name]['value'])
                / max(abs(own[name]['value']), abs(opponent[name]['value']), 3 if name.endswith('(%)') else 2)
                for name in common}
        for side in ('strength', 'weakness'):
            supporting = {name for name in common if _supports(name, own[name], opponent[name], side)}
            opposing = {name for name in common if _supports(
                name, own[name], opponent[name], 'weakness' if side == 'strength' else 'strength')}
            for names in chain(combinations(sorted(common), 3), combinations(sorted(common), 2)):
                if len({_family(name) for name in names}) != len(names):
                    continue
                evidence = supporting.intersection(names)
                if len(evidence) < 2 or (topic in TOPIC_ANCHORS and not TOPIC_ANCHORS[topic].intersection(evidence)):
                    continue
                if opposing.intersection(names):
                    continue
                score = sum(gaps[name] for name in evidence) / len(evidence) * (.92 if len(names) == 2 else 1)
                families = frozenset(_family(name) for name in names)
                by_topic[topic].append((score, topic, side, names, evidence, families))

    # Keep a few distinct evidence sets per topic so the fixture can reuse fewer
    # metrics without enumerating every combination from broad categories.
    for topic, options in by_topic.items():
        most_metrics = {}
        for option in options:
            side, names = option[2:4]
            most_metrics[side] = max(most_metrics.get(side, 0), len(names))
        options = [row for row in options if len(row[3]) == most_metrics[row[2]]]
        options.sort(key=lambda row: (-row[0], row[2], row[3]))
        retained = []
        for side in most_metrics:
            unique = {}
            for option in options:
                families = option[5]
                if option[2] != side or families in unique:
                    continue
                if len(unique) < 3 or any(not families.intersection(prior) for prior in unique):
                    unique[families] = option
                if len(unique) == 5:
                    break
            retained.extend(unique.values())
        by_topic[topic] = retained
    if len(by_topic) < 2:
        return [(option,) for options in by_topic.values() for option in options] or [()]
    return [pair for left, right in combinations(by_topic.values(), 2)
            for pair in product(left, right)]


def _fixture_pair_score(pairs, goal_margin):
    cards = [card for pair in pairs for card in pair]
    topics = {card[1] for card in cards}
    families = set().union(*(card[5] for card in cards))
    repeated_metrics = sum(len(card[5]) for card in cards) - len(families)
    result_fit = 0
    if goal_margin:
        winner, loser = pairs if goal_margin > 0 else pairs[::-1]
        result_fit = int(any(card[2] == 'strength' for card in winner))
        result_fit += int(any(card[2] == 'weakness' for card in loser))
    return result_fit, len(topics), sum(len(card[3]) == 3 for card in cards), -repeated_metrics, sum(card[0] for card in cards)


def _cards(selected, own, opponent, lang):
    locale = 0 if lang == 'tr' else 1
    return [{
        'id': topic, 'side': side, 'title': CATALOG[topic][side][locale],
        'description': CATALOG[topic]['description'][locale],
        'metrics': [{'metric_name': name, 'team': own[name], 'opponent': opponent[name],
                     'context_only': name not in evidence}
                    for name in sorted(names, key=lambda name: (name not in evidence, -_gap(name, own, opponent), name))],
    } for _, topic, side, names, evidence, _ in sorted(selected, key=lambda card: -card[0])]


def _gap(name, own, opponent):
    return abs(own[name]['value'] - opponent[name]['value']) / max(abs(own[name]['value']), abs(opponent[name]['value']), 1)


def upgrade_match_team_findings(content):
    if content.get('report_type') == 'pre_match' or content.get('match_team_findings_version') == VERSION:
        return content
    teams = content.get('teams') or []
    result = {**content, 'match_team_findings_version': VERSION,
              'match_team_findings': {}, 'match_team_comparison_ranks': {}}
    if len(teams) == 2:
        first, second = map(_match_metrics, teams)
        own, other = _comparison_metrics(first, second), _comparison_metrics(second, first)
        _attach_match_percentiles(content, ((teams[0]['id'], own), (teams[1]['id'], other)))
        rank_fields = ('match_position', 'season_position', 'total_teams',
                       'percentile', 'season_percentile', 'direction')
        result['match_team_comparison_ranks'] = {
            str(team['id']): {
                name: {key: metric[key] for key in rank_fields if key in metric}
                for name, metric in sample.items() if 'match_position' in metric
            } for team, sample in zip(teams, (own, other))
        }
        lang = content.get('language') or 'en'
        scores = {str(row.get('participant_id')): number((row.get('score') or {}).get('goals'))
                  for row in content.get('scores') or [] if row.get('description') == 'CURRENT'}
        first_goals, second_goals = (scores.get(str(team['id'])) for team in teams)
        goal_margin = first_goals - second_goals if first_goals is not None and second_goals is not None else None
        first_pair, second_pair = max(
            product(_candidate_pairs(own, other), _candidate_pairs(other, own)),
            key=lambda pairs: _fixture_pair_score(pairs, goal_margin))
        result['match_team_findings'] = {
            str(teams[0]['id']): _cards(first_pair, own, other, lang),
            str(teams[1]['id']): _cards(second_pair, other, own, lang),
        }
    return result
