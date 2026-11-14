"""Coach style fits from recorded team and outfield-player statistics.

All 18 archetypes are evaluated. These are metric-based style matches: aggregate
statistics do not measure exact defensive lines, marking assignments or the time
between losing the ball and applying pressure. Missing signals reduce coverage;
they never become fabricated zeroes. Bounds are scoring scales, not league ranks.
"""
import math
from statistics import mean, pstdev

# signal: (source metric, lower/upper scoring anchors, inverted, variation)
SIGNALS = {
    'control': ('Ball Possession %', 35, 65, False, False),
    'circulation': ('Passes', 250, 650, False, False),
    'accuracy': ('Accurate Passes (%)', 65, 90, False, False),
    'long': ('Long Ball Share (%)', 5, 22, False, False),
    'final_third': (('Final-third Pass Share (%)', 'Outfield Final-third Pass Share (%)'), 5, 25, False, False),
    'through': ('Through Balls', 0, 5, False, False),
    'inside': ('Inside-box Shot Share (%)', 35, 80, False, False),
    'creation': ('Key Passes', 3, 15, False, False),
    'carrying': ('Successful Dribbles', 2, 15, False, False),
    'dribble_success': ('Dribble Accuracy (%)', 35, 75, False, False),
    'crossing': ('Cross Share (%)', 1, 7, False, False),
    'cross_volume': ('Total Crosses', 5, 30, False, False),
    'counter': ('Counter Attacks', 0, 5, False, False),
    'shot_pace': ('Shots Per 100 Passes', 1.5, 5, False, False),
    'creator_focus': ('Creator Concentration (%)', 15, 60, False, False),
    'mid_creation': ('role:midfielder:Key Passes', 20, 65, False, False),
    'forward_carry': ('role:attacker:Successful Dribbles', 20, 70, False, False),
    'mid_pass': ('role:midfielder:Passes', 20, 55, False, False),
    'back_pass': ('role:defender:Passes', 15, 50, False, False),
    'tackle_activity': (('Tackles Per 100 Opponent Passes', 'Outfield Tackles Per 100 Opponent Passes'), 1.5, 6, False, False),
    'intercept_activity': (('Interceptions Per 100 Opponent Passes', 'Outfield Interceptions Per 100 Opponent Passes'), .5, 4, False, False),
    'recovery_activity': (('Recoveries Per 100 Opponent Passes', 'Outfield Recoveries Per 100 Opponent Passes'), 2, 12, False, False),
    'duel_activity': (('Duels Per 100 Opponent Passes', 'Outfield Duels Per 100 Opponent Passes'), 8, 30, False, False),
    'foul_activity': (('Fouls Per 100 Opponent Passes', 'Outfield Fouls Per 100 Opponent Passes'), 1, 4, False, False),
    'duel_success': (('Duels Won (%)', 'Outfield Duels Won (%)'), 35, 65, False, False),
    'clearances': (('Clearances', 'Outfield Clearances'), 5, 35, False, False),
    'blocks': ('Blocked Shots', 1, 6, False, False),
    'opp_offside': ('Opponent Offsides', 0, 4, False, False),
    'opp_long': ('Opponent Long Ball Share (%)', 5, 22, False, False),
    'opp_accuracy': ('Opponent Accurate Passes (%)', 65, 90, False, False),
    'opp_progress': ('Opponent Final-third Pass Share (%)', 5, 25, False, False),
    'opp_inside': ('Opponent Inside-box Shot Share (%)', 35, 80, False, False),
    'opp_creation': ('Opponent Key Passes', 3, 15, False, False),
    'opp_shots': ('Opponent Shots Total', 5, 20, False, False),
    'opp_counter': ('Opponent Counter Attacks', 0, 5, False, False),
    'mid_intercepts': ('role:midfielder:Interceptions', 20, 65, False, False),
    'mid_recovery': ('role:midfielder:Ball Recovery', 20, 60, False, False),
    'forward_recovery': ('role:attacker:Ball Recovery', 0, 25, False, False),
    'forward_tackles': ('role:attacker:Tackles', 0, 25, False, False),
    'back_recovery': ('role:defender:Ball Recovery', 20, 65, False, False),
    'back_clearances': ('role:defender:Clearances', 40, 90, False, False),
    'back_duels': ('role:defender:Duels Won', 15, 55, False, False),
    'role_balance': ('Defensive Role Balance (%)', 40, 95, False, False),
    'recovery_losses': (('Recovery To Loss Ratio', 'Outfield Recovery To Loss Ratio'), .1, .8, False, False),
    'long_variation': ('Long Ball Share (%)', .10, .65, False, True),
    'width_variation': ('Cross Share (%)', .10, .65, False, True),
    'control_variation': ('Ball Possession %', .03, .25, False, True),
    'threat_variation': ('Opponent Shots Total', .10, .65, False, True),
    'recovery_variation': ('role:midfielder:Ball Recovery', .10, .65, False, True),
    'intercept_variation': ('role:defender:Interceptions', .10, .65, False, True),
}
# A ! prefix means the lower end of that measured signal supports the style.
WEIGHTS = {
    'attack': {
        'positional': {'accuracy': .25, 'control': .20, 'mid_pass': .20, 'back_pass': .15, '!long': .10, 'final_third': .10},
        'possession': {'control': .40, 'circulation': .30, 'accuracy': .15, '!long': .15},
        'direct': {'long': .40, 'shot_pace': .25, '!circulation': .20, 'forward_carry': .15},
        'transition': {'counter': .35, '!control': .20, 'shot_pace': .20, 'forward_carry': .15, 'recovery_activity': .10},
        'combination': {'accuracy': .25, '!long': .25, 'mid_creation': .25, 'creation': .15, 'mid_pass': .10},
        'wide': {'crossing': .40, 'cross_volume': .25, 'carrying': .20, 'forward_carry': .15},
        'vertical': {'through': .30, 'final_third': .30, 'inside': .20, 'shot_pace': .20},
        'pragmatic': {'long_variation': .35, 'width_variation': .35, 'control_variation': .30},
        'free': {'creator_focus': .35, 'carrying': .25, 'dribble_success': .20, 'forward_carry': .20},
    },
    'defence': {
        'aggressive': {'tackle_activity': .30, 'duel_activity': .25, 'forward_tackles': .20, 'foul_activity': .15, '!opp_accuracy': .10},
        'high': {'opp_offside': .30, 'control': .20, '!opp_progress': .20, 'opp_long': .15, 'forward_recovery': .15},
        'midblock': {'mid_intercepts': .30, 'mid_recovery': .25, 'intercept_activity': .20, '!opp_creation': .15, '!clearances': .10},
        'deep': {'clearances': .30, 'blocks': .25, 'back_clearances': .20, '!control': .15, 'opp_inside': .10},
        'man': {'duel_activity': .30, 'duel_success': .25, 'back_duels': .20, 'tackle_activity': .15, 'foul_activity': .10},
        'zone': {'intercept_activity': .25, 'role_balance': .25, '!foul_activity': .20, '!opp_inside': .15, '!opp_creation': .15},
        'counterpress': {'recovery_activity': .25, 'forward_recovery': .20, 'mid_recovery': .15, 'recovery_losses': .20, '!opp_counter': .10, '!foul_activity': .10},
        'reactive': {'!control': .30, 'opp_shots': .25, 'back_recovery': .20, 'blocks': .15, '!forward_tackles': .10},
        'pragmatic': {'threat_variation': .35, 'recovery_variation': .35, 'intercept_variation': .30},
    },
}


def signal_values(catalog):
    result = {}
    for name, (key, low, high, inverse, variation) in SIGNALS.items():
        if isinstance(key, tuple):
            key = max(key, key=lambda candidate: sum(r.get('covered',1) for r in catalog.get(candidate,{}).get('seasons',[])))
        rows = [r for r in catalog.get(key, {}).get('seasons', [])
                if isinstance(r.get('value'), (int, float)) and math.isfinite(r['value'])]
        if not rows:
            continue
        if variation:
            values = [r['value'] for r in rows]
            # Match-to-match noise alone gets half the weight of period variation.
            raw = pstdev(values) / max(abs(mean(values)), .001) if len(values) > 1 else rows[0].get('matchVariation', 0) * .5
            if not math.isfinite(raw):
                continue
        else:
            raw = mean(r['value'] for r in rows)
        normalized = max(0., min(1., (raw - low) / (high - low)))
        coverage = mean(min(1., r.get('covered', 1) / max(1, r.get('matches', 1))) for r in rows)
        result[name] = {'metric': key, 'value': raw, 'score': 1 - normalized if inverse else normalized, 'coverage': coverage}
    return result


def rank_styles(catalog, side):
    signals = signal_values(catalog)
    ranked = []
    for identity, weights in WEIGHTS[side].items():
        evidence, weighted, observed = [], 0., 0.
        for name, weight in weights.items():
            signal = signals.get(name.lstrip('!'))
            if signal is None:
                continue
            fit = 1 - signal['score'] if name.startswith('!') else signal['score']
            effective = weight * signal['coverage']
            weighted += fit * effective
            observed += effective
            evidence.append({**signal, 'signal': name, 'weight': weight, 'support': round(fit, 3)})
        # Missing evidence reduces a fit instead of silently rewarding a low/zero value.
        score = weighted / observed * math.sqrt(observed / sum(weights.values())) if observed else 0.
        ranked.append({'id': identity, 'statisticalFit': round(score, 4), 'coverage': round(observed / sum(weights.values()), 3), 'signals': evidence})
    return sorted(ranked, key=lambda row: (-row['statisticalFit'], -row['coverage'], row['id']))
