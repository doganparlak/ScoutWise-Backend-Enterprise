"""Deterministic league-fit verdict using similarity's role/subgroup weights.

Bands are product rules for a weighted percentile index, not calibrated transfer
probabilities or the player's overall percentile rank. Zeroes remain evidence.
"""
from bisect import bisect_left, bisect_right
import re

from fastapi import HTTPException
from scoutwise_pro_module.discovery import ALIASES, BUNDLES, player_metrics
from scoutwise_pro_module.similarity_weights import feature_weights
from scoutwise_pro_module.team_fit import eligible_roles

METRICS = {name.lstrip('-') for names in BUNDLES.values() for name in names}
NEGATIVE = {name[1:] for names in BUNDLES.values() for name in names if name.startswith('-')}
BANDS = ((80, 'very_strong'), (65, 'strong'), (45, 'conditional'), (30, 'limited'), (0, 'weak'))
VERDICT_LABELS = {
    'very_strong': {'tr': 'Üstün uyum', 'en': 'Superior fit'},
    'strong': {'tr': 'Yüksek uyum', 'en': 'High fit'},
    'conditional': {'tr': 'Koşullu uyum', 'en': 'Conditional fit'},
    'limited': {'tr': 'Sınırlı uyum', 'en': 'Limited fit'},
    'weak': {'tr': 'Zayıf uyum', 'en': 'Weak fit'},
}


def metric_key(value):
    return re.sub(r'[^a-z0-9]', '', value.lower().replace('%', 'percentage').replace('&', 'and'))


CANONICAL = {metric_key(name): name for name in METRICS | {'Minutes Played'}}
CANONICAL.update({metric_key(alias): name for name, aliases in ALIASES.items() for alias in aliases})


def profile_metrics(profile):
    canonical = {CANONICAL[metric_key(key)]: value for key, value in profile.items() if metric_key(key) in CANONICAL}
    canonical['match_count'] = profile.get('match_count')
    return player_metrics(canonical)


def weighted_verdict(candidate, cohort):
    roles = eligible_roles(candidate)
    values = profile_metrics(candidate)
    peers = [profile_metrics(peer) for peer in cohort]
    return weighted_percentile_verdict(values, peers, roles)


def weighted_percentile_verdict(values, peers, roles):
    """Already normalized per-90 measurements; never compare team totals."""
    def canonical(values):
        return {CANONICAL[metric_key(key)]: value for key, value in values.items()
                if metric_key(key) in CANONICAL and isinstance(value, (float, int))}
    values = canonical(values)
    peers = [canonical(peer) for peer in peers]
    distributions = {metric: sorted(peer[metric] for peer in peers if metric in peer)
                     for metric in values}
    distributions = {metric: population for metric, population in distributions.items() if population}
    weights = feature_weights(values, distributions, roles)
    # Keep missing evidence from silently producing a decisive verdict from a
    # tiny fraction of the role profile. Use similarity's 70% weighted coverage.
    full_weights = feature_weights(dict.fromkeys(METRICS, 1), dict.fromkeys(METRICS, 1), roles)
    coverage = sum(weight for metric, weight in full_weights.items() if metric in weights)
    if not weights or coverage + 1e-9 < .70:
        raise HTTPException(status_code=422, detail='Insufficient role-weighted evidence for fit')
    percentiles = {}
    for metric in weights:
        population, value = distributions[metric], values[metric]
        left = bisect_left(population, value - 1e-9)
        right = bisect_right(population, value + 1e-9)
        percentile = (left + right) / (2 * len(population)) * 100
        percentiles[metric] = 100 - percentile if metric in NEGATIVE else percentile
    score = sum(weights[metric] * percentiles[metric] for metric in weights)
    verdict = next(key for floor, key in BANDS if score + 1e-9 >= floor)
    return {'key': verdict, 'score': score, 'coverage': coverage,
            'roleShares': roles, 'metricWeights': weights, 'percentiles': percentiles,
            'metricEvidence': [{'metric': metric, 'values': [values[metric], sum(distributions[metric]) / len(distributions[metric])],
                                'percentile': percentiles[metric], 'weight': weights[metric],
                                'lowerIsBetter': metric in NEGATIVE, 'unit': 'percentage' if '%' in metric else 'per90'}
                               for metric in weights]}
