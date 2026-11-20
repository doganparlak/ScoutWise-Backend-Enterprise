"""Evidence-led findings for Enterprise pre-match reports."""
import hashlib
import json
import logging
import math
from itertools import combinations

from report_module.profile_generation import generate_profile_json
from report_module.team_findings import CATALOG, INTERPRETATION_FOCUS, number
from standings_module.metrics import LOWER_BETTER
from standings_module.team_derived import DEFINITIONS


FINDINGS_VERSION = 14
logger = logging.getLogger(__name__)

# Topic boundaries keep the candidate pool relevant. The model still chooses
# which of the eligible measurements best tell the story of this fixture.
TOPIC_METRICS = {
    'creation': {'Key Passes', 'Big Chances Created', 'Expected Goals (xG)', 'Expected Goals',
                 'Expected Goals Open Play (xGOP)', 'Expected Goals Non Penalty Goals (npxG)',
                 'Shots Insidebox', 'Shots Total', 'Dangerous Attacks', 'Expected Assists'},
    'finishing': {'Goals', 'Goal Conversion (%)', 'On Target Goal Conversion (%)',
                  'Shots On Target (%)', 'Shots On Target'},
    'passing': {'Accurate Passes (%)', 'Passes', 'Accurate Passes', 'Possession Lost', 'Dispossessed'},
    'progression': {'Total Crosses', 'Final-third Pass Share (%)', 'Passes In Final Third', 'Outfield Final-third Pass Share (%)',
                    'Outfield Passes In Final Third', 'Accurate Crosses', 'Accurate Crosses (%)', 'Unsuccessful Crosses',
                    'Accurate Long Balls', 'Long Balls Won (%)', 'Dangerous Attacks'},
    'dribbling': {'Dribble Accuracy (%)', 'Successful Dribbles', 'Dribbles Won', 'Dispossessed'},
    'set_pieces': {'Expected Goals Set Play (xGSP)', 'Expected Goals Corners (xGC)',
                   'Expected Goals Free Kicks (xGFK)', 'Corners'},
    'duels': {'Duels Won (%)', 'Aerials Won (%)', 'Duels Won', 'Aerials Won', 'Duels Lost', 'Aerials Lost'},
    'recoveries': {'Ball Recovery', 'Interceptions', 'Tackles Won', 'Recoveries Per 100 Opponent Passes',
                   'Interceptions Per 100 Opponent Passes', 'Tackles Won (%)'},
    'threat': {name for name, (_, category, _) in DEFINITIONS.items()
               if category == 'opponent_threat' and name not in ('Opponent Big Chances Missed', 'Opponent Hit Woodwork')},
    'errors': {'Error Lead To Goal', 'Error Lead To Shot', 'Own Goals', 'Dispossessed', 'Possession Lost'},
    'discipline': {'Fouls', 'Fouls Per 100 Opponent Passes', 'Yellow Cards', 'Red Cards', 'Total Cards',
                   'Penalties Committed'},
    'game_management': {'Lead Win Rate (%)', 'Trailing Equalizer Rate (%)'},
}
TOPIC_ANCHORS = {
    'passing': {'Accurate Passes (%)', 'Possession Lost', 'Dispossessed'},
    'finishing': {'Goals', 'Goal Conversion (%)', 'On Target Goal Conversion (%)', 'Shots On Target (%)'},
    'dribbling': {'Dribble Accuracy (%)', 'Successful Dribbles'},
    'set_pieces': {'Expected Goals Set Play (xGSP)', 'Expected Goals Corners (xGC)', 'Expected Goals Free Kicks (xGFK)'},
    'errors': {'Error Lead To Goal', 'Error Lead To Shot', 'Own Goals'},
}


def _preference(name):
    if name in DEFINITIONS:
        return DEFINITIONS[name][2]
    if name in LOWER_BETTER or name in {'Fouls Per 100 Opponent Passes', 'Total Cards'}:
        return 'lower'
    return 'higher'


def _supports(name, own, opponent, side):
    if name == 'Total Crosses':
        return False  # Delivery volume contextualizes accuracy and completion.
    preference = _preference(name)
    if preference == 'neutral':
        return False
    gap = own['value'] - opponent['value']
    if preference == 'lower':
        gap = -gap
    if side == 'weakness':
        gap = -gap
    # Do not turn negligible execution-rate differences into a finding.
    return gap >= 0.5 if name.endswith('(%)') else gap > 0


def _eligible_metrics(left, right, side):
    eligible = {}
    for topic, pool in TOPIC_METRICS.items():
        available = pool & left.keys() & right.keys()
        supporting = {name for name in available if _supports(name, left[name], right[name], side)}
        if (len(available) >= 3 and len(supporting) >= 2
                and (topic not in TOPIC_ANCHORS or TOPIC_ANCHORS[topic].intersection(supporting))):
            eligible[topic] = {'supporting': sorted(supporting), 'context': sorted(available - supporting)}
    return eligible


def _available(usage):
    sample_size = usage.get('sample_size') or 0
    minimum = max(3, math.ceil(sample_size * .6))
    metrics = {}
    for rows in (usage.get('team_comparison') or {}).values():
        for row in rows:
            value, covered = number(row.get('value')), number(row.get('matches_covered'))
            if value is not None and covered is not None and covered >= minimum:
                metric = {'value': value, 'matches_covered': int(covered)}
                rank = row.get('competition_rank') or {}
                total, position = number(rank.get('total_teams')), number(rank.get('position'))
                if total is not None and position is not None and 1 <= position <= total and total > 1:
                    metric['percentile'] = round(100 * (total - position) / (total - 1))
                metrics[row['name']] = metric
    # Older snapshots may have the complete source counts but no derived rate.
    derived = {
        'On Target Goal Conversion (%)': ('Goals', 'Shots On Target'),
        'Goal Conversion (%)': ('Goals', 'Shots Total'),
        'Fouls Per 100 Opponent Passes': ('Fouls', 'Opponent Passes'),
        'Recoveries Per 100 Opponent Passes': ('Ball Recovery', 'Opponent Passes'),
        'Interceptions Per 100 Opponent Passes': ('Interceptions', 'Opponent Passes'),
    }
    for name, (numerator, denominator) in derived.items():
        top, bottom = metrics.get(numerator), metrics.get(denominator)
        if (name not in metrics and top and bottom and bottom['value'] > 0 and
                top['matches_covered'] == bottom['matches_covered'] == sample_size and
                (not name.endswith('(%)') or top['value'] <= bottom['value'])):
            metrics[name] = {'value': round(100 * top['value'] / bottom['value'], 4), 'matches_covered': sample_size}
    states = {row.get('key'): row for row in (usage.get('score_flow') or {}).get('states', [])}
    for state, field, name in (
        ('ahead', 'final_wins', 'Lead Win Rate (%)'),
        ('behind', 'matches_to_level', 'Trailing Equalizer Rate (%)'),
    ):
        row = states.get(state, {})
        count, wins = number(row.get('matches_entered')), number(row.get(field))
        if count is not None and wins is not None and 0 <= wins <= count and count >= 3:
            metrics[name] = {'value': round(100 * wins / count, 4), 'matches_covered': int(count)}
    return metrics



def _bind_findings(draft, samples, lang, allowed_metrics):
    by_id = {sample['id']: sample for sample in samples}
    findings = {team_id: {'strength': [], 'weakness': []} for team_id in by_id}
    used_topics = {team_id: set() for team_id in by_id}
    used_comparisons = {}
    locale = 0 if lang == 'tr' else 1

    for row in draft['themes']:
        if not isinstance(row, dict):
            continue
        team_id = str(row.get('team_id'))
        side, topic = row.get('side'), row.get('topic')
        if team_id not in by_id or side not in ('strength', 'weakness') or not isinstance(topic, str):
            continue
        if topic not in CATALOG:
            topic = next((key for key, label in CATALOG.items() if topic in label[side]), topic)
        sample = by_id[team_id]
        opponent = next(other for other in samples if other['id'] != team_id)
        cards = findings[team_id][side]
        names = row.get('metrics')
        if (topic not in CATALOG or topic in used_topics[team_id] or len(cards) == 2
                or not isinstance(names, list) or not all(isinstance(name, str) for name in names)
                or len(set(names)) != 3 or len(names) != 3
                or not all(name in sample['metrics'] and name in opponent['metrics'] for name in names)):
            continue
        metrics = [{'metric_name': name, 'team': sample['metrics'][name],
                    'opponent': opponent['metrics'][name]} for name in names]
        eligible = allowed_metrics[team_id][side].get(topic)
        if not eligible:
            continue
        supporting = set(names).intersection(eligible['supporting'])
        if (len(supporting) < 2 or not set(names).issubset(eligible['supporting'] + eligible['context'])
                or (topic in TOPIC_ANCHORS and not TOPIC_ANCHORS[topic].intersection(supporting))):
            continue
        metrics.sort(key=lambda metric: metric['metric_name'] not in supporting)
        for metric in metrics:
            if metric['metric_name'] not in supporting:
                metric['context_only'] = True
        comparison = (topic, side)
        evidence = set(names)
        prior = used_comparisons.get(comparison)
        if prior and (prior[0] <= evidence or evidence <= prior[0]):
            opposite_cards = findings[prior[1]][side]
            opposite_cards[:] = [card for card in opposite_cards if card['id'] != topic]
            continue
        used_comparisons[comparison] = (evidence, team_id)
        used_topics[team_id].add(topic)
        cards.append({
            'id': topic, 'title': CATALOG[topic][side][locale],
            'description': (
                ('Rakibin hücum girişimlerinin ve bu girişimlerin oluşturduğu tehlikenin değerlendirilmesi.',
                 'Assessment of opposition attacking actions and the danger they create.')[locale]
                if topic == 'threat' else
                ('Kanat servislerinin isabeti ve başarısız orta yükü.',
                 'Accuracy of wide deliveries and frequency of unsuccessful crosses.')[locale]
                if topic == 'progression' and 'Unsuccessful Crosses' in names else CATALOG[topic]['description'][locale]
            ),
            'metrics': metrics,
        })
    return findings


def _finding_drafts(findings):
    return [
        {'team_id': team_id, 'side': side, 'topic': card['id'],
         'metrics': [metric['metric_name'] for metric in card['metrics']]}
        for team_id, sides in findings.items()
        for side, cards in sides.items()
        for card in cards
    ]


def findings_complete(findings):
    return len(findings) == 2 and all(
        len(sides.get(side, ())) == 2
        for sides in findings.values() for side in ('strength', 'weakness')
    )


def build_pre_match_findings(usages, lang, teams=(), previous=None):
    if len(usages) != 2:
        return {}
    teams_by_id = {str(team['id']): team for team in teams}
    samples = []
    for usage in usages:
        team_id = str(usage['team_id'])
        team = teams_by_id.get(team_id, {})
        samples.append({'id': team_id, 'name': team.get('name'),
                        'sample_size': usage.get('sample_size'), 'metrics': _available(usage)})
    if any(not sample['name'] or not sample['metrics'] for sample in samples):
        return {}
    left, right = (sample['metrics'] for sample in samples)
    shared = {
        name for name in left.keys() & right.keys()
        if 'percentile' in left[name] and 'percentile' in right[name]
        and (left[name]['value'] != right[name]['value'] or left[name]['percentile'] != right[name]['percentile'])
    }
    if len(shared) < 3:
        return {}
    for sample in samples:
        sample['metrics'] = {name: sample['metrics'][name] for name in sorted(shared)}

    allowed_metrics = {
        team['id']: {side: _eligible_metrics(team['metrics'], samples[1 - index]['metrics'], side)
                     for side in ('strength', 'weakness')}
        for index, team in enumerate(samples)
    }
    for sides in allowed_metrics.values():
        # Reserve a topic for the side with fewer alternatives when the other
        # side can still fill both of its slots.
        for side, opposite in (('strength', 'weakness'), ('weakness', 'strength')):
            scarce, other = sides[side], sides[opposite]
            if len(scarce) <= 2 and len(other.keys() - scarce.keys()) >= 2:
                sides[opposite] = {topic: names for topic, names in other.items() if topic not in scarce}
    context = {
        'language': lang,
        'teams': samples,
        'allowed_metrics': allowed_metrics,
        'catalog': {topic: {
            'strength': row['strength'], 'weakness': row['weakness'],
            'focus': INTERPRETATION_FOCUS[topic],
        } for topic, row in CATALOG.items()},
    }
    messages = [
        ('system', """You are ScoutWise Enterprise's pre-match football analyst. Return JSON with a themes array.
Choose TWO strengths and TWO weaknesses for EACH team, using the catalog topic keys. Each theme has team_id, side (strength or weakness), topic, and metrics (exactly THREE distinct supplied metric names). Do not return prose. Treat the task as complete only when all eight themes are supplied, if data supports them.
Choose topics and metrics only from allowed_metrics for the specific team and side. Choose at least two names from supporting. Use a third supporting metric when relevant, or one name from context when it clarifies the other two. Context is never evidence for the heading. Season percentiles provide context and cannot reverse the direction. Use complementary measures and prioritize direct outcome or efficiency evidence over volume. Do not use a topic more than once per team."""),
        ('human', json.dumps(context, ensure_ascii=False)),
    ]
    accepted = _finding_drafts(previous or {})
    bound = _bind_findings({'themes': accepted}, samples, lang, allowed_metrics)
    for _ in range(2):
        missing = [
            {'team_id': team_id, 'side': side, 'count': 2 - len(cards)}
            for team_id, sides in bound.items()
            for side, cards in sides.items() if len(cards) < 2
            and (allowed_metrics[team_id][side].keys()
                 - {card['id'] for existing in sides.values() for card in existing})
        ]
        if not missing:
            break
        task = {
            'missing_slots': missing,
            'existing_findings': {team_id: {side: [card['id'] for card in cards]
                                           for side, cards in sides.items()}
                                  for team_id, sides in bound.items()},
        }
        followup = (
            'Return additions only for missing_slots. Preserve all existing findings and use a distinct topic. '
            'Only use allowed_metrics for each missing team and side. Select two supporting metrics and at most one context metric. '
        )
        try:
            draft = generate_profile_json([*messages, ('human', followup + json.dumps(task))])
            proposed = _bind_findings({'themes': [*accepted, *draft['themes']]}, samples, lang, allowed_metrics)
            if all(len(proposed[team][side]) >= len(cards)
                   for team, sides in bound.items() for side, cards in sides.items()):
                bound = proposed
                accepted = _finding_drafts(bound)
        except Exception:
            logger.exception('Pre-match findings generation failed')
            break
    if not findings_complete(bound):
        # Fill from the validated pool if the model leaves slots open. In a
        # sparse matchup a topic can be usable on either side, so allocate the
        # two sets jointly before selecting any replacement metrics.
        fallback = []
        for index, sample in enumerate(samples):
            team_id = sample['id']
            pools = allowed_metrics[team_id]
            opponent_metrics = samples[1 - index]['metrics']
            existing = {side: {card['id']: card for card in cards}
                        for side, cards in bound[team_id].items()}
            choices = {
                side: [topics for count in range(min(2, len(pool)) + 1)
                       for topics in combinations(pool, count)]
                for side, pool in pools.items()
            }
            strength, weakness = max(
                ((a, b) for a in choices['strength'] for b in choices['weakness'] if set(a).isdisjoint(b)),
                key=lambda pair: (sum(map(len, pair)), min(map(len, pair)),
                                  sum(topic in existing[side]
                                      for side, topics in zip(('strength', 'weakness'), pair)
                                      for topic in topics)),
            )
            for side, topics in (('strength', strength), ('weakness', weakness)):
                for topic in topics:
                    card = existing[side].get(topic)
                    if card:
                        names = [row['metric_name'] for row in card['metrics']]
                    else:
                        pool = pools[side][topic]
                        supporting = pool['supporting']
                        anchors = TOPIC_ANCHORS.get(topic, set())
                        def relevance(name):
                            own = sample['metrics'][name]['value']
                            other = opponent_metrics[name]['value']
                            gap = abs(own - other) / max(abs(own), abs(other), 1e-9)
                            return name not in anchors, -gap, name

                        names = sorted(supporting, key=relevance)[:3]
                        if len(names) == 2:
                            context_names = pool['context']
                            preferred = 'Total Crosses' if topic == 'progression' and all('Crosses' in name for name in names) else None
                            names.append(min(context_names, key=lambda name: (name != preferred, *relevance(name))))
                    fallback.append({'team_id': team_id, 'side': side, 'topic': topic, 'metrics': names})
        bound = _bind_findings({'themes': fallback}, samples, lang, allowed_metrics)
    return bound if any(cards for sides in bound.values() for cards in sides.values()) else {}


def enrich_pre_match_findings(content):
    usage = content.get('recent_squad_usage')
    previous = (usage or {}).get('team_findings') or {}
    version = (usage or {}).get('team_findings_version')
    if not usage or (version == FINDINGS_VERSION and findings_complete(previous)):
        return content
    findings = build_pre_match_findings(usage.get('teams') or [], content.get('language', 'en'), content.get('teams') or [],
                                        previous=previous if version in (9, 10, 11, 12, 13, FINDINGS_VERSION) else None)
    return {**content, 'recent_squad_usage': {
        **usage, 'team_findings': findings, 'team_findings_version': FINDINGS_VERSION if findings else 0,
    }}

def tactical_plan_key(usage, lang):
    findings = usage.get('team_findings') or {}
    if len(findings) != 2 or not any(cards for sides in findings.values() for cards in sides.values()):
        return None
    source = json.dumps([lang, findings], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(source.encode()).hexdigest()


TACTICAL_PLAN_GUIDANCE = """A strategy is one coherent paragraph of 50–60 words in the requested language. Use the existing plan's practical logic: explain how to defend, progress the ball, create an advantage and adapt to the score against the named opponent. Ground the actions in the supplied selected strengths and risks of the team and opponent. Turn the labels into concrete tactical choices and priorities. Do not recite label names, metrics, statistics or numerical values. Never infer a formation, pressing scheme, player trait or established tendency from a metric alone. Present recommendations as recommendations. Use the named opponent explicitly and respect the listed venue. In Turkish use formal recommendations such as daraltılmalı or kullanılabilir, with no direct commands, headings or markdown."""


def attach_tactical_plans(usage, rows, teams, lang):
    key = tactical_plan_key(usage, lang)
    if not key or len(teams) != 2 or not isinstance(rows, list):
        return usage
    plans = {
        str(row['team_id']): row['strategy'].strip()
        for row in rows if isinstance(row, dict) and 'team_id' in row
        and isinstance(row.get('strategy'), str) and 35 <= len(row['strategy'].split()) <= 80
    }
    if set(plans) == {str(team['id']) for team in teams} and all(
        opponent.get('name') and opponent['name'].casefold() in plans[str(team['id'])].casefold()
        for team, opponent in (teams, teams[::-1])
    ):
        return {**usage, 'team_tactical_plans': plans, 'team_tactical_plans_key': key}
    return usage


def enrich_tactical_plans(usage, teams, lang):
    key = tactical_plan_key(usage, lang)
    if usage.get('team_tactical_plans_key') and usage['team_tactical_plans_key'] != key:
        usage = {**usage, 'team_tactical_plans': {}, 'team_tactical_plans_key': None}
    if not key or usage.get('team_tactical_plans_key') == key or len(teams) != 2:
        return usage
    findings = usage['team_findings']
    team_analysis = usage.get('team_analysis') or {}
    evidence = []
    for team in teams:
        team_id = str(team['id'])
        evidence.append({
            'team_id': team_id, 'name': team.get('name'), 'venue': team.get('location'),
            'existing_plan': (team_analysis.get(team_id) or {}).get('strategy'),
            'findings': {side: [
                {'label': card['title'], 'description': card['description'],
                 'evidence': [metric for metric in card['metrics'] if not metric.get('context_only')]}
                for card in cards
            ] for side, cards in findings.get(team_id, {}).items()},
        })
    try:
        draft = generate_profile_json([
            ('system', "You are ScoutWise Enterprise's senior pre-match analyst. Return JSON with a themes array "
                       "containing one object per team, with team_id and strategy. " + TACTICAL_PLAN_GUIDANCE),
            ('human', json.dumps({'language': lang, 'teams': evidence}, ensure_ascii=False)),
        ])
        return attach_tactical_plans(usage, draft['themes'], teams, lang)
    except Exception:
        logger.exception('Findings-based tactical plan generation failed')
    return usage
