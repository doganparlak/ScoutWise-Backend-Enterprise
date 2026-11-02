"""Serving-only v5 transforms; keep numerical parity with mobile runtime."""
import math
import numpy as np

LEGACY_STATS = ('Shots On Target', 'Shots Total', 'Shots Insidebox', 'Big Chances Created', 'Ball Possession %', 'Expected Goals')


ADVANCED_STATS = ('Expected Goals On Target', 'Expected Goals Non Penalty Goals (npxG)',
    'Expected Goals Open Play (xGOP)', 'Expected Goals Set Play (xGSP)', 'Expected Goals Corners (xGC)',
    'Big Chances Missed', 'Dangerous Attacks', 'Attacks', 'Corners', 'Shots Off Target', 'Shots Blocked',
    'Shots Outsidebox', 'Successful Passes Percentage', 'Passes', 'Key Passes', 'Long Passes',
    'Successful Long Passes Percentage', 'Total Crosses', 'Accurate Crosses', 'Tackles', 'Tackles Won',
    'Interceptions', 'Successful Dribbles', 'Duels Won', 'Saves', 'Fouls', 'Yellowcards', 'Redcards')


PRESSURE_STATS = ('Pressure Net', 'Pressure Dominance', 'Pressure Late Net')


STAT_NAMES = LEGACY_STATS + ADVANCED_STATS + PRESSURE_STATS


ALIASES = {'Expected Goals (xG)': 'Expected Goals', 'Expected Goals on Target (xGoT)': 'Expected Goals On Target',
           'Expected Goals On Target (xGoT)': 'Expected Goals On Target', 'Shots Inside Box': 'Shots Insidebox',
           'Accurate Passes Percentage': 'Successful Passes Percentage', 'Red Cards': 'Redcards'}


CORE = ('gf', 'ga', 'recent_gf', 'recent_ga', 'opp_gf', 'opp_ga', 'opp_recent_gf', 'opp_recent_ga',
        'home', 'league_home_goals', 'league_away_goals', 'rating', 'opponent_rating')


VENUE = ('venue_gf', 'venue_ga', 'opp_venue_gf', 'opp_venue_ga')


SCHEDULE = ('rest_days', 'matches_7_days', 'matches_14_days', 'opp_rest_days', 'opp_matches_7_days', 'opp_matches_14_days')


STRATEGIES = ('legacy', 'recent', 'opponent', 'venue', 'schedule')


def schema(strategy, stats):
    names = list(CORE)
    if strategy in ('venue', 'schedule'):
        names += VENUE
    if strategy == 'schedule':
        names += SCHEDULE
    names += [f'{side}:{name}' for side in ('own', 'opponent', 'opp_own', 'opp_opponent') for name in stats]
    return names


def _mean(rows, key, weighted, adjusted=False):
    from .forecast import WINDOW
    rows = rows[-WINDOW:]
    available = []
    for index, row in enumerate(rows):
        value = row.get(key) if ':' not in key else row.get(key.split(':', 1)[0], {}).get(key.split(':', 1)[1])
        if value is None or not math.isfinite(float(value)):
            continue
        weight = 2 ** (-(len(rows) - 1 - index) / 5.) if weighted else 1.
        # Pre-match opponent rating is stored at the historical kickoff. We
        # adjust attacking production up against stronger opponents, and vice versa.
        if adjusted and (key in ('gf', 'ga') or key.split(':')[-1] in ('Expected Goals', 'Expected Goals On Target', 'Shots On Target', 'Shots Total', 'Shots Insidebox', 'Big Chances Created')):
            strength = row.get('opponentStrength', 0.)
            attacking = key == 'gf' or key.startswith('own:')
            value *= math.exp((1 if attacking else -1) * strength)
        available.append((float(value), weight))
    if not available or (':' in key and len(available) < max(3, math.ceil(len(rows) * .6))):
        return None
    return sum(v * w for v, w in available) / sum(w for _, w in available)


def summary(history, home, kickoff, strategy):
    from .forecast import utc, WINDOW
    rows = history[-WINDOW:]
    weighted = strategy != 'legacy'
    adjusted = strategy in ('opponent', 'venue', 'schedule')
    result = {key: _mean(rows, key, weighted, adjusted) for key in ('gf', 'ga')}
    for side in ('own', 'opponent'):
        for name in STAT_NAMES:
            result[f'{side}:{name}'] = _mean(rows, f'{side}:{name}', weighted, adjusted)
    for key in ('gf', 'ga'):
        result[f'recent_{key}'] = _mean(rows[-3:], key, weighted, adjusted)
        if strategy in ('venue', 'schedule'):
            venue = [r for r in rows if r.get('home') is home]
            specific = _mean(venue, key, weighted, adjusted)
            overall = result[key]
            strength = len(venue) / (len(venue) + 5.)
            result[f'venue_{key}'] = overall if specific is None else strength * specific + (1 - strength) * overall
    if strategy == 'schedule' and kickoff and rows:
        days = [(utc(kickoff) - utc(row['at'])).total_seconds() / 86400 for row in rows]
        if min(days) <= 0:
            raise ValueError('Forecast history crosses kickoff')
        result.update(rest_days=min(min(days), 30.), matches_7_days=sum(d <= 7 for d in days), matches_14_days=sum(d <= 14 for d in days))
    return result


def values(own, opponent, home, context, strategy, names):
    context = context or {}
    a = summary(own, home, context.get('kickoff'), strategy)
    b = summary(opponent, not home, context.get('kickoff'), strategy)
    merged = {**a, **{f'opp_{key}': value for key, value in b.items()}, 'home': float(home), **context}
    return [merged.get(name) for name in names]


def bounded_rho(home, away, rho):
    # Strict positivity for all four affected cells, even at extreme goal rates.
    return max(-.99 / max(home, away), min(float(rho), .99, .99 / (home * away)))


def dc_factor(home, away, hg, ag, rho):
    rho = bounded_rho(home, away, rho)
    if (hg, ag) == (0, 0): return 1 - home * away * rho
    if (hg, ag) == (0, 1): return 1 + home * rho
    if (hg, ag) == (1, 0): return 1 + away * rho
    if (hg, ag) == (1, 1): return 1 - rho
    return 1.


def rate(model, own, opponent, home, context):
    raw = np.array(values(own, opponent, home, context, model['strategy'], model['featureNames']), dtype=float)[model['columns']]
    missing = ~np.isfinite(raw)
    raw = np.where(missing, model['medians'], raw)
    x = np.concatenate([[1.], (raw - model['mean']) / model['scale'], missing.astype(float)])
    value = float(np.exp(np.clip(x @ np.array(model['beta']), math.log(.05), math.log(8.))))
    factor = model.get('goalRateCalibration', {}).get('home' if home else 'away', {}).get('factor', 1.)
    return min(8., max(.05, value * factor))
