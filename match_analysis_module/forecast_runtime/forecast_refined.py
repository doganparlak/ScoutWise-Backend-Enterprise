"""Serving-only v5 transforms; keep numerical parity with mobile runtime."""
from collections import OrderedDict
import base64
import json
import math
from threading import Lock
import zlib
import numpy as np
from . import forecast as f, forecast_candidates as c

KIND = 'refined_rate_ensemble_v1'


_STATE_CACHE = OrderedDict()


_STATE_LOCK = Lock()


def restore_state(payload):
    if payload.get('encoding') != 'zlib-json-v1':
        return c.History.restore(payload)
    encoded = payload['payload']
    with _STATE_LOCK:
        decoded = _STATE_CACHE.get(encoded)
    if decoded is None:
        decoded = json.loads(zlib.decompress(base64.b64decode(encoded)))
        with _STATE_LOCK:
            _STATE_CACHE[encoded] = decoded
            _STATE_CACHE.move_to_end(encoded)
            while len(_STATE_CACHE) > 3:
                _STATE_CACHE.popitem(last=False)
    return c.History.restore(decoded)


def predict_rates(model, fixture, cutoff, base_rates):
    component = model['refinedEnsemble']
    if component['kind'] != KIND:
        raise ValueError('Unsupported refined ensemble')
    weights = component['weights']
    if set(weights) != {'incumbent', 'nonlinear'} or min(weights.values()) < 0 or not math.isclose(sum(weights.values()), 1):
        raise ValueError('Invalid refined ensemble weights')
    if not weights['nonlinear']:
        return np.asarray(base_rates, dtype=float)
    state = restore_state(component['state'])
    if state.last_available and state.last_available > cutoff:
        raise ValueError('Refined ensemble history crosses prediction cutoff')
    home, away = fixture['homeTeam']['id'], fixture['awayTeam']['id']
    if min(len(state.histories[home]), len(state.histories[away])) < f.MIN_HISTORY:
        raise ValueError('Insufficient refined history')
    raw = np.array([c.features(state, home, away, True, fixture['league']['id'], fixture['startingAt'], model['strategy']),
                    c.features(state, away, home, False, fixture['league']['id'], fixture['startingAt'], model['strategy'])], float)
    tree_rates = c.tree_predict(component['nonlinear'], raw)
    return weights['incumbent'] * np.asarray(base_rates) + weights['nonlinear'] * tree_rates
