"""Serving-only v5 transforms; keep numerical parity with mobile runtime."""
from collections import defaultdict
import math
import numpy as np
from . import forecast as f, forecast_learning as l

BLOCKS = ('ratings', 'windows', 'league_home', 'red_cards')


FORM_KEYS = ('gf', 'ga', 'own:Expected Goals', 'opponent:Expected Goals',
             'own:Shots On Target', 'opponent:Shots On Target', 'own:Pressure Net')


HALF_KEYS = ('hgf', 'hga', 'second_gf', 'second_ga')


def _value(row, key):
    if key == 'second_gf':
        return None if row.get('hgf') is None else row['gf'] - row['hgf']
    if key == 'second_ga':
        return None if row.get('hga') is None else row['ga'] - row['hga']
    if ':' in key:
        side, name = key.split(':', 1)
        return row.get(side, {}).get(name)
    return row.get(key)


def _mean(rows, key, red=False):
    available = []
    for row in rows:
        value = _value(row, key)
        if value is None or not math.isfinite(float(value)):
            continue
        weight = 1 - .7 * row.get('red_exposure', 0.) if red else 1.
        available.append((float(value), weight))
    if not available or (':' in key and len(available) < max(3, math.ceil(len(rows) * .6))):
        return None
    return sum(v * w for v, w in available) / sum(w for _, w in available)


class History:

    def __init__(self):
        self.histories = defaultdict(list)
        self.totals = {}
        self.ratings = {}
        self.strengths = {}
        self.half_totals = {}
        self.seen = set()
        self.last_available = None

    @classmethod
    def restore(cls, payload):
        result = cls()
        result.histories = defaultdict(list, {int(k): list(v) for k, v in payload['histories'].items()})
        result.totals = {int(k): list(v) for k, v in payload['totals'].items()}
        result.ratings = {int(k): v for k, v in payload['ratings'].items()}
        result.strengths = {int(k): v for k, v in payload['strengths'].items()}
        result.half_totals = {int(k): list(v) for k, v in payload['halfTotals'].items()}
        result.seen = set(payload['seen'])
        result.last_available = f.utc(payload['lastAvailableAt']) if payload.get('lastAvailableAt') else None
        return result


def features(state, team, other, home, league, kickoff, strategy):
    own, opponent = state.histories[team], state.histories[other]
    context = {**f._context(state.totals, state.ratings, league, team, other), 'kickoff': kickoff}
    values = l.values(own, opponent, home, context, strategy, l.schema(strategy, l.STAT_NAMES))
    values += [*state.strengths.get(team, (0., 0.)), *state.strengths.get(other, (0., 0.))]
    for rows in (own, opponent):
        for window in (5, 10, None):
            sample = rows[-window:] if window else rows
            values.extend(_mean(sample, key) for key in FORM_KEYS)
    lh, la = context['league_home_goals'], context['league_away_goals']
    values += [lh if home else la, la if home else lh, (1 if home else -1) * math.log(lh / la)]
    for rows in (own, opponent):
        values.extend(_mean(rows[-10:], key, red=True) for key in FORM_KEYS[:6])
    for rows in (own, opponent):
        known = [row for row in rows[-10:] if row.get('red_known')]
        values.extend(sum(row.get('early_' + side, 0) > 0 for row in known) / len(known) if known else None for side in ('own', 'opp'))
    for rows in (own, opponent):
        for window in (5, 10, None):
            sample = rows[-window:] if window else rows
            values.extend(_mean(sample, key) for key in HALF_KEYS)
    values += [f._half_prior(state.half_totals, league, home), f._half_prior(state.half_totals, league, not home)]
    return values


def tree_predict(model, raw):
    x = np.asarray(raw, dtype=float)[:, model['columns']]
    eta = np.full(len(x), model['offset'])
    for tree in model['trees']:
        for index, row in enumerate(x):
            node_index = 0
            while True:
                value, feature, threshold, missing_left, left, right, leaf = tree[node_index]
                if leaf:
                    eta[index] += value
                    break
                number = row[feature]
                node_index = left if (missing_left if np.isnan(number) else number <= threshold) else right
    return np.exp(np.clip(eta, math.log(.05), math.log(8.)))
