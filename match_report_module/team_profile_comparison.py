"""League comparison for the selected-match team report."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta
from itertools import combinations, product
from statistics import mean, median
import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api_module.database import benchmark_engine
from report_module.metric_validation import is_standard_count
from report_module.rate_evidence import RATES, add_sample, empty, key as rate_key, missing_attempt_names, rate_value

from match_analysis_module.pre_match_findings import TOPIC_ANCHORS, TOPIC_METRICS, _preference, _supports
from report_module.team_findings import CATALOG, CATALOG_VERSION
from standings_module.comparison_ranks import canonical_metric_name
from standings_module.metrics import LOWER_BETTER, kickoff, numeric
from standings_module.team_derived import DEFINITIONS, build_team_derived, metric_sort_direction
from standings_module.stages import EUROPEAN_LEAGUES, stage_scope
from .style_profile_ranks import lineup_style_ranks
from .style_profiles import TASKS
from .team_findings import _comparison_metrics, _family, _match_metrics

DISPLAY_VERSION = 11
_LOG = logging.getLogger(__name__)
_BENCHMARK_ERRORS = (SQLAlchemyError, OSError, RuntimeError, ValueError, TypeError, KeyError)


def visual_profile(styles, shortlist, cell, version, side):
    themes = [{**{key: style[key] for key in ('styleId', 'styleSide', 'title', 'description')},
               'metrics': list(style['source_metrics'].values()), 'analysis': []} for style in styles[:2]]
    players = []
    for player in shortlist:
        source = {canonical_metric_name(name): (name, detail)
                  for rows in player['metrics'].values() for name, detail in rows.items()}
        available = [(name, weight, family) for name, weight, family in TASKS[side] if name in source]
        available.sort(key=lambda item: (-item[1], -source[item[0]][1]['value'], item[0]))
        chosen = {}
        for name, _, family in available:
            chosen.setdefault(family, name)
        if len(chosen) < 2:
            continue
        metrics = []
        for canonical in list(chosen.values())[:4]:
            name, detail = source[canonical]
            metrics.append({**cell(name, detail['value'], detail['basis']),
                            'sourceKey': canonical, 'basis': detail['basis']})
        players.append({**{key: player.get(key) for key in ('playerId', 'name', 'imageUrl', 'position', 'minutes', 'averageRating')},
                        'metrics': metrics, 'analysis': []})
        if len(players) == 2:
            break
    return {'styleVersion': version, 'themes': themes, 'players': players}


def _competition_scopes(reports):
    scopes = defaultdict(list)
    for report in reports:
        league, season = report.get('league') or {}, report.get('season') or {}
        league_id, season_id = league.get('id'), season.get('id')
        starts_at = (report.get('fixture') or {}).get('starting_at')
        if league_id and season_id and starts_at:
            phase = ('qualifying' if league_id in EUROPEAN_LEAGUES
                     and stage_scope(report.get('stage') or {}) == 'qualifying' else 'overall')
            scopes[(league_id, season_id, phase)].append(report)
    return scopes


def _competition_badge(league, season, phase, reports):
    competition = reports[0]['league']
    return {'key': f'{league}:{season}:{phase}', 'id': league, 'name': competition.get('name') or '',
            'imageUrl': competition.get('image_path'), 'season': reports[0]['season'].get('name'),
            'qualifying': phase == 'qualifying', 'matchCount': len({r['fixture']['id'] for r in reports
                                                                  if r['fixture'].get('id')})}


def _percentile(values, value, direction):
    first, last = bisect_left(values, value), bisect_right(values, value)
    rank = (first + last) / 2
    return round(100 * (len(values) - rank if direction == 'asc' else rank) / len(values))


def _paired_performance(reports, team_id):
    own, other = defaultdict(list), defaultdict(list)
    foul_samples = ([], [])
    for report in reports:
        teams = report.get('teams') or []
        team = next((row for row in teams if row.get('id') == team_id), None)
        opponent = next((row for row in teams if row.get('id') != team_id), None)
        if not team or not opponent:
            continue
        first, second = _match_metrics(team), _match_metrics(opponent)
        left, right = _comparison_metrics(first, second), _comparison_metrics(second, first)
        for samples, side, against in ((foul_samples[0], first, second), (foul_samples[1], second, first)):
            fouls, passes = side.get('Fouls'), against.get('Passes')
            if fouls and passes and fouls['value'] >= 0 and passes['value'] > 0:
                samples.append((fouls['value'], passes['value']))
        for name in left.keys() & right.keys():
            own[name].append(left[name]['value'])
            other[name].append(right[name]['value'])
    minimum = max(1, (len(reports) + 1) // 2)
    results = tuple({name: {'value': sum(items) / len(items), 'matches_covered': len(items)}
                     for name, items in sample.items() if len(items) >= minimum}
                    for sample in (own, other))
    for sample, samples in zip(results, foul_samples):
        name = 'Fouls Per 100 Opponent Passes'
        sample.pop(name, None)
        if len(samples) >= minimum:
            sample[name] = {'value': 100 * sum(a for a, _ in samples) / sum(b for _, b in samples),
                            'matches_covered': len(samples)}
    return results


def _finding_metrics(own, opponent):
    metrics = {}
    for name in set().union(*TOPIC_METRICS.values()) & own.keys() & opponent.keys():
        value = own[name]['value']
        if value <= 0:
            continue
        metrics[name] = {'label': name, 'sourceKey': name,
                         'value': f'{value:.2f}' + ('%' if '(%)' in name else ''),
                         'basis': ('per100OpponentPasses' if name.endswith(' Per 100 Opponent Passes')
                                   else 'rate' if '(%)' in name else 'perMatch'),
                         'matchesCovered': own[name]['matches_covered']}
    return metrics


def _finding_options(own, opponent, lang, metrics):
    groups = {'strength': [], 'weakness': []}
    locale = 0 if lang == 'tr' else 1
    for topic, pool in TOPIC_METRICS.items():
        for side in groups:
            evidence = []
            for name in sorted(pool & metrics.keys()):
                supports = _supports(name, own[name], opponent[name], side)
                opposing_side = 'weakness' if side == 'strength' else 'strength'
                value, other_value = own[name]['value'], opponent[name]['value']
                metric = metrics[name]
                gap = abs(value - other_value) / max(abs(value), abs(other_value), 2)
                rank = next((group['percentile'] for group in metric.get('competitionMatches', [])), None)
                if _preference(name) == 'neutral' or name == 'Total Crosses':
                    evidence.append((name, {**metric, 'detail': 'context'}, 0, False))
                elif rank is None and supports:
                    evidence.append((name, metric, gap, True))
                elif rank is not None:
                    edge = rank - 50 if side == 'strength' else 50 - rank
                    if edge > 0:
                        evidence.append((name, metric, edge / 50, True))
                    elif edge == 0:
                        evidence.append((name, {**metric, 'detail': 'context'}, 0, False))
                elif gap <= .075 and not _supports(name, own[name], opponent[name], opposing_side):
                    evidence.append((name, {**metric, 'detail': 'context'}, 0, False))
            evidence.sort(key=lambda row: (-row[2], row[0]))
            options = []
            for size in (3, 2):
                for selected in combinations(evidence, size):
                    names = {row[0] for row in selected if row[3]}
                    if not names or len(names) < size - 1:
                        continue
                    selected_names = {row[0] for row in selected}
                    if ('Expected Goals Set Play (xGSP)' in selected_names
                            and selected_names & {'Expected Goals Corners (xGC)', 'Expected Goals Free Kicks (xGFK)'}):
                        continue
                    families = {_family(name) for name in selected_names}
                    anchored = topic not in TOPIC_ANCHORS or bool(TOPIC_ANCHORS[topic] & names)
                    # Conversion per shot and per shot on target answer distinct
                    # questions, but share a goal numerator. Use the pair only
                    # if both support the label and stronger evidence is scarce.
                    conversion_pair = names == selected_names == {
                        'Goal Conversion (%)', 'On Target Goal Conversion (%)'}
                    if (len(families) != size and not conversion_pair) or (not anchored and len(names) < 2):
                        continue
                    score = sum(row[2] for row in selected) / size + .05 * (size == 3)
                    options.append((score - .25 * (not anchored) - .25 * conversion_pair - .35 * (len(names) == 1), families, {
                        'findingId': topic, 'findingSide': side, 'title': CATALOG[topic][side][locale],
                        'description': CATALOG[topic]['description'][locale],
                        'metrics': [row[1] for row in selected], 'analysis': [],
                    }))
            options.sort(key=lambda row: -row[0])
            for size in (3, 2):
                groups[side].extend([option for option in options if len(option[2]['metrics']) == size][:4])
    pairs = []
    for side in ('strength', 'weakness'):
        options = groups[side]
        candidates = [pair for pair in combinations(options, 2) if pair[0][2]['findingId'] != pair[1][2]['findingId']]
        pairs.append([*candidates, *((option,) for option in options), ()])

    def selection_score(pair):
        cards = (*pair[0], *pair[1])
        distinct = len({entry[2]['findingId'] for entry in cards})
        metrics = set().union(*(entry[1] for entry in cards))
        return (distinct, sum(len(entry[2]['metrics']) == 3 for entry in cards),
                -(sum(len(entry[1]) for entry in cards) - len(metrics)), sum(entry[0] for entry in cards))

    compatible = (pair for pair in product(*pairs)
                  if not {item[2]['findingId'] for item in pair[0]} & {item[2]['findingId'] for item in pair[1]})
    strengths, weaknesses = max(compatible, key=selection_score)
    # With scarce evidence, never show the same topic with opposing labels.
    strong_ids = {item[2]['findingId'] for item in strengths}
    return ({'themes': [item[2] for item in strengths], 'catalogVersion': CATALOG_VERSION, 'displayVersion': DISPLAY_VERSION},
            {'themes': [item[2] for item in weaknesses if item[2]['findingId'] not in strong_ids], 'catalogVersion': CATALOG_VERSION, 'displayVersion': DISPLAY_VERSION})


def _match_values(raw):
    evidence = empty()
    add_sample(evidence, raw)
    values = {canonical_metric_name(name): value for name, value in raw.items()
              if rate_key(name) not in RATES and numeric(value) is not None}
    for name, rate in evidence['rates'].items():
        value = rate_value(name, rate)
        if value is not None:
            values[name] = value
    return {rate_key(name): float(value) for name, value in values.items()}


def _player_match_values(entries, targets, scoreless_teams=()):
    """Include omitted zero counts when the fixture provides coverage for them."""
    if not targets:
        return
    covered_counts = {
        rate_key(name) for _, _, raw in entries for name, value in raw.items()
        if is_standard_count(name) and numeric(value) is not None and rate_key(name) in targets
    }
    # Goalkeeper-only inputs aren't comparable across outfield appearances.
    covered_counts.difference_update({'saves', 'goals_conceded', 'penalty_saves', 'goals_prevented'})
    for player_id, team_id, raw in entries:
        values = _match_values(raw)
        recorded = {rate_key(name) for name in raw}
        coverage = covered_counts | ({'goals'} if team_id in scoreless_teams and 'goals' in targets else set())
        zeros = coverage - recorded - missing_attempt_names(raw)
        yield player_id, team_id, {**dict.fromkeys(zeros, 0.0), **values}, zeros


def _scoreless_teams(scores):
    return {
        int(score['participant_id']) for score in scores
        if score.get('description') == 'CURRENT' and score.get('participant_id')
        and numeric((score.get('score') or {}).get('goals')) == 0
    }


def _competition_match_rows(league, season, phase, cutoff, include_players):
    # Stream saved contributions once for all four sections and their PDF pages.
    player_column = 'player_contributions' if include_players else "'[]'::jsonb AS player_contributions"
    with benchmark_engine.connect() as db:
        yield from db.execute(text(f"""
            SELECT fixture_id, team_contributions, {player_column}
            FROM league_match_contributions
            WHERE league_id=:league AND season_id=:season AND included_in_aggregation=TRUE
            AND kickoff_at<:cutoff AND (NOT :european OR
                (:phase='qualifying' AND coverage->>'scope'='qualifying') OR
                (:phase='overall' AND coverage->>'scope' IN ('league','knockout')))
        """).execution_options(stream_results=True, yield_per=20),
            dict(league=league, season=season, cutoff=cutoff, phase=phase,
                 european=league in EUROPEAN_LEAGUES)).mappings()


def _attach_competition_matches(reports, team_id, profiles):
    targets = defaultdict(list)
    for profile in profiles:
        for cards, player in ((profile['themes'], False), (profile.get('players', []), True)):
            for card in cards:
                player_id = int(card['playerId']) if player else None
                for metric in card['metrics']:
                    name = canonical_metric_name(metric.get('sourceKey') or metric['label'])
                    if name.startswith('Rakipler · '):
                        name = 'Opponent ' + name.removeprefix('Rakipler · ')
                    targets[player_id, name].append(metric)
    if not targets:
        return
    team_keys = {rate_key(name) for player, name in targets if player is None}
    player_keys = {rate_key(name) for player, name in targets if player is not None}
    for (league, season, phase), selected in _competition_scopes(reports).items():
        fixture_ids = {int(report['fixture']['id']) for report in selected if report['fixture'].get('id')}
        cutoff = max(kickoff(report['fixture']['starting_at']) for report in selected) + timedelta(minutes=1)
        badge = _competition_badge(league, season, phase, selected)
        cohorts = {False: defaultdict(list), True: defaultdict(list)}
        samples = defaultdict(dict)

        def record(values, selected_player, fixture_id, player=False):
            for key in (player_keys if player else team_keys) & values.keys():
                value = values[key]
                cohorts[player][key].append(value)
                if selected_player is not False and fixture_id in fixture_ids:
                    samples[selected_player, key][fixture_id] = value

        try:
            for row in _competition_match_rows(league, season, phase, cutoff, bool(player_keys)):
                fixture_id = row['fixture_id']
                derived = build_team_derived([row], include_profile_rates=True)['teams']
                for tid, contribution in (row['team_contributions'] or {}).items():
                    values = _match_values(contribution.get('metrics') or {})
                    values.update({rate_key(name): metric['perMatch']
                                   for name, metric in derived.get(str(tid), {}).items()})
                    record(values, None if int(tid) == team_id else False, fixture_id)
                entries = [
                    (int(player['playerId']), int(player['teamId']),
                     {**dict.fromkeys(player.get('unknownMetrics') or []), **(player.get('metrics') or {})})
                    for player in row['player_contributions'] or []
                    if (numeric(player.get('minutes')) or 0) > 0
                ]
                scoreless = {
                    int(tid) for tid, contribution in (row['team_contributions'] or {}).items()
                    if numeric((contribution.get('metrics') or {}).get('Goals')) == 0
                }
                for player_id, player_team, values, _ in _player_match_values(entries, player_keys, scoreless):
                    record(values, player_id if player_team == team_id else False, fixture_id, player=True)
        except _BENCHMARK_ERRORS:
            _LOG.warning('Match distributions unavailable for team report (%s)', league)
            cohorts = {False: defaultdict(list), True: defaultdict(list)}
        if any(name.startswith(('role:', 'Outfield ')) for player, name in targets if player is None):
            try:
                lineup_style_ranks(league, season, cutoff, phase, on_match=lambda fid, tid, name, value:
                    record({rate_key(name): value}, None if int(tid) == team_id else False, fid))
            except _BENCHMARK_ERRORS:
                _LOG.warning('Lineup match distributions unavailable for team report (%s)', league)
        # Keep recorded selected-match values available when the benchmark feed is sparse.
        for report in selected:
            fixture_id = int(report['fixture'].get('id') or 0)
            if not fixture_id:
                continue
            teams = report.get('teams') or []
            own = next((team for team in teams if int(team.get('id') or 0) == team_id), None)
            opponent = next((team for team in teams if int(team.get('id') or 0) != team_id), None)
            if own is not None and opponent is not None:
                values = _comparison_metrics(_match_metrics(own), _match_metrics(opponent))
                for name, metric in values.items():
                    key = rate_key(name)
                    if key in team_keys:
                        samples[None, key].setdefault(fixture_id, metric['value'])
            entries = []
            for player in report.get('lineups') or []:
                player_id, player_team = int(player.get('player_id') or 0), int(player.get('team_id') or 0)
                raw = {
                    canonical_metric_name(row['name']): row.get('value')
                    for rows in [*(player.get('categories') or {}).values(), player.get('expected_metrics') or []]
                    for row in rows if row.get('name') and not row.get('_inferred_zero')
                }
                if player_id and (numeric(raw.get('Minutes Played')) or 0) > 0:
                    entries.append((player_id, player_team, raw))
            for player_id, player_team, values, inferred in _player_match_values(
                entries, player_keys, _scoreless_teams(report.get('scores') or []),
            ):
                if player_team != team_id:
                    continue
                for key in player_keys & values.keys():
                    metric_samples = samples[player_id, key]
                    if key in inferred:
                        metric_samples.setdefault(fixture_id, values[key])
                    else:
                        metric_samples[fixture_id] = values[key]
        for by_metric in cohorts.values():
            for values in by_metric.values():
                values.sort()
        for (player_id, name), cells in targets.items():
            key = rate_key(name)
            values = samples[player_id, key]
            if not values:
                continue
            cohort = cohorts[player_id is not None].get(key, [])
            direction = metric_sort_direction(name) if name in DEFINITIONS else (
                'asc' if name in LOWER_BETTER or name == 'Fouls Per 100 Opponent Passes' else 'desc')
            percentiles = [_percentile(cohort, value, direction) for value in values.values()] if len(cohort) >= 5 else []
            group = {**badge, 'value': round(mean(values.values()), 4), 'covered': len(values),
                     'percentile': median(percentiles) if percentiles else None, 'matchPercentiles': percentiles,
                     'benchmarkSize': len(cohort)}
            for cell in cells:
                cell.setdefault('competitionMatches', []).append(group)


_RECORDED_PROFILE_DIMENSIONS = {
    'attack': (
        ('attack_output', 'Hücum Üretimi', 'Attacking Output',
         'Şut hacmi, fırsat kalitesi ve gol üretimi.', 'Shot volume, chance quality and scoring.',
         ('Expected Goals', 'Shots Insidebox', 'Goals', 'Big Chances Created', 'Shots On Target', 'Shots Total')),
        ('attack_progression', 'Hücumda İlerleme', 'Attacking Progression',
         'Pas ve driplinglerle hücuma katkı.', 'Progression through passing and dribbling.',
         ('Passes In Final Third', 'Key Passes', 'Successful Dribbles', 'Accurate Crosses', 'Dangerous Attacks', 'Passes')),
        ('attack_scoring', 'Gol Üretimi', 'Scoring Output',
         'Seçili maçlardaki gol üretimi.', 'Scoring across the selected matches.',
         ('Goals',)),
        ('attack_scoring_consistency', 'Gol Bulma Sürekliliği', 'Scoring Consistency',
         'Gol bulunan maçların seçili maçlara oranı.', 'Share of selected matches with a goal scored.',
         ('Scoring Matches (%)',)),
    ),
    'defence': (
        ('defence_resistance', 'Rakibe Verilen Fırsatlar', 'Chances Allowed',
         'Rakibin şutları, fırsat kalitesi ve gol üretimi.', 'Opposition shots, chance quality and scoring.',
         ('Expected Goals Against (xGA)', 'Opponent Shots Insidebox', 'Goals Conceded', 'Opponent Big Chances Created',
          'Opponent Shots On Target', 'Opponent Shots Total')),
        ('defence_actions', 'Savunma Müdahaleleri', 'Defensive Actions',
         'Top kazanımları ve savunma müdahalelerinin maç başı görünümü.', 'Ball wins and interventions per match.',
         ('Ball Recovery', 'Interceptions', 'Tackles Won', 'Clearances', 'Blocks', 'Tackles')),
        ('defence_goals_allowed', 'Gol Yeme Profili', 'Goals Allowed',
         'Seçili maçlarda yenilen goller.', 'Goals conceded across the selected matches.',
         ('Goals Conceded',)),
        ('defence_clean_sheets', 'Gol Yemeden Tamamlama', 'Clean Sheets',
         'Gol yenmeyen maçların seçili maçlara oranı.', 'Share of selected matches without conceding.',
         ('Clean Sheets (%)',)),
    ),
}


def _recorded_profile_themes(reports, team_id, lang, side, existing=()):
    """Describe observable output when the tactical classifier lacks coverage."""
    samples = defaultdict(list)
    for report in reports:
        teams = {int(team['id']): team for team in report.get('teams', []) if team.get('id')}
        opponent = next((tid for tid in teams if tid != team_id), None)
        own = _match_metrics(teams.get(team_id, {}))
        other = _match_metrics(teams.get(opponent, {}))
        values = {name: metric['value'] for name, metric in _comparison_metrics(own, other).items()}
        if 'Goals' in other:
            values.setdefault('Goals Conceded', other['Goals']['value'])
        for score in report.get('scores') or []:
            if score.get('description') != 'CURRENT':
                continue
            participant = int(score.get('participant_id') or 0)
            goals = numeric((score.get('score') or {}).get('goals'))
            if goals is not None and participant == team_id:
                values['Goals'] = goals
            elif goals is not None and participant == opponent:
                values['Goals Conceded'] = goals
        if 'Goals' in values:
            values['Scoring Matches (%)'] = 100.0 * (values['Goals'] > 0)
        if 'Goals Conceded' in values:
            values['Clean Sheets (%)'] = 100.0 * (values['Goals Conceded'] == 0)
        for name, value in values.items():
            samples[name].append(value)

    themes = list(existing)
    used = {metric.get('sourceKey') or metric['label'] for theme in themes for metric in theme['metrics']}
    for identity, title_tr, title_en, description_tr, description_en, names in _RECORDED_PROFILE_DIMENSIONS[side]:
        if len(themes) >= 2:
            break
        available = sorted((name for name in names if name not in used and samples.get(name)),
                           key=lambda name: (-len(samples[name]), names.index(name)))[:4]
        if not available or (len(names) > 1 and available in (['Goals'], ['Goals Conceded'])):
            continue
        used.update(available)
        themes.append({'styleId': identity, 'styleSide': side, 'title': title_tr if lang == 'tr' else title_en,
                       'description': description_tr if lang == 'tr' else description_en, 'analysis': [],
                       'metrics': [{'label': name, 'sourceKey': name, 'value': str(round(mean(samples[name]), 4)),
                                    'basis': 'rate' if name.endswith('(%)') else 'perMatch', 'contributes': True,
                                    'matchesCovered': len(samples[name])} for name in available]})
    return themes


def enrich_team_profiles(reports, team_id, lang, attack, defence):
    from .report import _team_player_metric_reports, build_team_report_attack_profile, build_team_report_defense_profile

    scopes = _competition_scopes(reports)
    if not scopes:
        for profile, side in ((attack, 'attack'), (defence, 'defence')):
            if len(profile['themes']) < 2:
                profile['themes'] = _recorded_profile_themes(reports, team_id, lang, side, profile['themes'])
        own, opponent = _paired_performance(reports, team_id)
        strengths, weaknesses = _finding_options(own, opponent, lang, _finding_metrics(own, opponent))
    else:
        attack_scopes, defence_scopes, strong_scopes, weak_scopes = [], [], [], []
        for (league, season, phase), selected in scopes.items():
            competition = _competition_badge(league, season, phase, selected)
            # The visual builders derive their style and player evidence directly from the fixtures.
            if len(selected) == len(reports):
                scoped_attack, scoped_defence = dict(attack), dict(defence)
            else:
                prepared_players = _team_player_metric_reports(selected)
                scoped_attack = build_team_report_attack_profile(
                    selected, team_id, {}, lang, include_analysis=False, minimum_style_metrics=3, prepared_player_reports=prepared_players,
                )
                scoped_defence = build_team_report_defense_profile(
                    selected, team_id, {}, lang, include_analysis=False, minimum_style_metrics=3, prepared_player_reports=prepared_players,
                )
            for profile, side in ((scoped_attack, 'attack'), (scoped_defence, 'defence')):
                if len(profile['themes']) < 2:
                    profile['themes'] = _recorded_profile_themes(selected, team_id, lang, side, profile['themes'])
            own, opponent = _paired_performance(selected, team_id)
            metrics = _finding_metrics(own, opponent)
            _attach_competition_matches(selected, team_id, (
                scoped_attack, scoped_defence, {'themes': [{'metrics': list(metrics.values())}]},
            ))
            for profile in (scoped_attack, scoped_defence):
                for theme in profile['themes']:
                    for metric in theme['metrics']:
                        value = numeric(str(metric.get('value', '')).strip('%'))
                        if not metric.get('competitionMatches') and value is not None and metric.get('matchesCovered'):
                            metric['competitionMatches'] = [{**competition, 'value': value,
                                'covered': metric['matchesCovered'], 'percentile': None, 'benchmarkSize': 0}]
            scoped_metrics = {name: metric for name, metric in metrics.items()
                              if any(group['covered'] >= max(1, (group['matchCount'] + 1) // 2)
                                     and group['value'] != 0 for group in metric.get('competitionMatches', []))}
            strength, weakness = _finding_options(own, opponent, lang, scoped_metrics)
            for rows, profile in ((attack_scopes, scoped_attack), (defence_scopes, scoped_defence),
                                  (strong_scopes, strength), (weak_scopes, weakness)):
                rows.append({'competition': competition, **profile})
        attack['scopedFindings'] = attack_scopes
        defence['scopedFindings'] = defence_scopes
        strengths, weaknesses = (
            {'themes': groups[0]['themes'], 'scopedFindings': groups, 'catalogVersion': CATALOG_VERSION}
            for groups in (strong_scopes, weak_scopes)
        )
    profiles = (attack, defence, strengths, weaknesses)
    for profile in profiles:
        profile['displayVersion'] = DISPLAY_VERSION
    return strengths, weaknesses


def upgrade_visual_team_profiles(content, team_id, lang):
    from .report import build_team_report_attack_profile, build_team_report_defense_profile

    fields = ('attackProfile', 'defenseProfile', 'strengths', 'weaknesses')
    reports = content.get('reports') or []
    if not reports or all((content.get(field) or {}).get('displayVersion') == DISPLAY_VERSION for field in fields):
        return content
    metrics = content.get('teamMetrics') or {}
    attack = build_team_report_attack_profile(reports, team_id, metrics, lang, include_analysis=False)
    defence = build_team_report_defense_profile(reports, team_id, metrics, lang, include_analysis=False)
    strengths, weaknesses = enrich_team_profiles(reports, team_id, lang, attack, defence)
    profiles = {'attack': attack, 'defense': defence, 'strengths': strengths, 'weaknesses': weaknesses}
    overview = []
    for row in content.get('overview') or []:
        profile = profiles.get(row.get('key'))
        if profile is None:
            overview.append(row)
            continue
        themes = profile['themes']
        overview.append({**row, 'profileLabelVersion': 1, 'subBullets': [],
                         'summary': ' · '.join(theme['title'] for theme in themes)})
    return {**content, **dict(zip(fields, (attack, defence, strengths, weaknesses))), 'overview': overview}
