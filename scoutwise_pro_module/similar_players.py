"""Deterministic role-filtered player similarity; no LLM or tactical inputs."""
from bisect import bisect_left, bisect_right
from math import sqrt
from fastapi import HTTPException
from pydantic import BaseModel, Field
from matchup_module.comparison import _fetch_player_metadata
from player_pool_module.player_pool import search_players
from scoutwise_pro_module.discovery import CATEGORY_DEFINITIONS, CONTEXT_BUNDLES, DiscoveryFilters, player_metrics, discovery_roles

from scoutwise_pro_module.similarity_weights import feature_weights

MIN_COVERAGE = 0.70
MIN_COMMON_METRICS = 15
MIN_CATEGORIES = 3
METRIC_CATEGORIES = {
    metric.lstrip('-'): category
    for category, _, _, groups in CATEGORY_DEFINITIONS
    for key, _, _, metrics in groups
    for metric in [*metrics, *CONTEXT_BUNDLES.get(f'{category}.{key}', [])]
}


def sufficient_metrics(metrics, goalkeeper):
    names = {m for m in metrics if m in METRIC_CATEGORIES
             and (goalkeeper or METRIC_CATEGORIES[m] != 'goalkeeping')}
    return len(names) >= MIN_COMMON_METRICS and len({METRIC_CATEGORIES[m] for m in names}) >= MIN_CATEGORIES


class SimilarFilters(DiscoveryFilters):
    # Reuse discovery validation, but do not allow a current-team restriction.
    team: list[str] = Field(default_factory=list, max_length=0)


class SimilarPlayersIn(BaseModel):
    playerId: int = Field(gt=0)
    filters: SimilarFilters = Field(default_factory=SimilarFilters)


def identity(row):
    return str(row['content'].get('player_id') or f"row:{row['id']}")


def rank_similar(source_row, rows):
    source = player_metrics(source_row['content'], include_context=True)
    role_shares = discovery_roles(source_row['content'])
    roles = set(role_shares)
    if not roles or not sufficient_metrics(feature_weights(source, source, role_shares), 'GK' in roles):
        raise HTTPException(status_code=422, detail='similarity_source_insufficient')
    eligible = {}
    for row in rows:
        if identity(row) == identity(source_row):
            continue
        matched = roles & set(discovery_roles(row['content']))
        values = player_metrics(row['content'], include_context=True)
        if not matched or not sufficient_metrics(values, 'GK' in roles):
            continue
        old = eligible.get(identity(row))
        # Duplicate providers/rows cannot give a player extra ranking opportunities.
        if old is None or (-len(values), int(row['id'])) < (-len(old['metrics']), int(old['row']['id'])):
            eligible[identity(row)] = {'row': row, 'metrics': values, 'roles': sorted(matched)}
    if not eligible:
        return []
    distributions = {}
    for metric in source:
        values = sorted([source[metric]] + [entry['metrics'][metric] for entry in eligible.values() if metric in entry['metrics']])
        if len(values) >= 2 and values[0] != values[-1]:
            distributions[metric] = values
    if not distributions:
        # Identical valid profiles still match when the entire pool is constant.
        distributions = {m: sorted([source[m]] + [entry['metrics'][m] for entry in eligible.values() if m in entry['metrics']]) for m in source}
        distributions = {m: values for m, values in distributions.items() if len(values) > 1}
    weights = feature_weights(source, distributions, role_shares)
    if not sufficient_metrics(weights, 'GK' in roles):
        return []
    def percentile(metric, value):
        values = distributions[metric]
        return (bisect_left(values, value) + bisect_right(values, value)) / (2 * len(values))
    target = {m: percentile(m, source[m]) for m in weights}
    ranked = []
    for entry in eligible.values():
        common = [m for m in weights if m in entry['metrics']]
        coverage = sum(weights[m] for m in common)
        if not sufficient_metrics(common, 'GK' in roles) or coverage + 1e-9 < MIN_COVERAGE:
            continue
        candidate = {m: percentile(m, entry['metrics'][m]) for m in common}
        dot = sum(weights[m] * target[m] * candidate[m] for m in common)
        norm = sqrt(sum(weights[m] * target[m] ** 2 for m in common) * sum(weights[m] * candidate[m] ** 2 for m in common))
        if norm <= 0:
            continue
        cosine = min(1.0, max(0.0, dot / norm))
        closeness = 1 - sum(weights[m] * abs(target[m] - candidate[m]) for m in common) / coverage
        score = round(100 * (0.7 * cosine + 0.3 * closeness), 1)
        ranked.append({'player': entry['row'], 'similarity': score, 'coverage': round(100 * coverage, 1), 'commonMetricCount': len(common), 'matchedRoles': entry['roles']})
    ranked.sort(key=lambda row: (-row['similarity'], -row['coverage'], int(row['player']['id'])))
    # Rank once; the client reveals this snapshot in batches of ten.
    return ranked[:50]


def load_similarity_source(db, player_id):
    try:
        source = _fetch_player_metadata(db, str(player_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail='Player not found') from exc
    return source


def source_is_eligible(source):
    roles = discovery_roles(source['content'])
    values = player_metrics(source['content'], include_context=True)
    weighted_metrics = feature_weights(values, values, roles)
    return bool(roles and sufficient_metrics(weighted_metrics, 'GK' in roles))


def similarity_eligibility(db, payload):
    # One player lookup only: never fetch/rank the candidate pool during precheck.
    source = load_similarity_source(db, payload.playerId)
    return {'eligible': source_is_eligible(source), 'minimumMinutes': 90,
            'minimumMetrics': MIN_COMMON_METRICS, 'minimumCategories': MIN_CATEGORIES}


def similar_players(db, payload):
    source = load_similarity_source(db, payload.playerId)
    # Revalidate at search time even when the client has passed the precheck.
    if not source_is_eligible(source):
        raise HTTPException(status_code=422, detail='similarity_source_insufficient')
    # Full database coverage; no discovery shortlist limit and no AI selection.
    filters = payload.filters.model_dump(mode='json', exclude_none=True)
    choices = {key: filters.pop(key, []) for key in ('nationality', 'team', 'league')}
    rows = search_players(db, filters, all_matches=True, candidate_roles=list(discovery_roles(source['content'])), candidate_choices=choices)
    return {'players': rank_similar(source, rows), 'minimumCoverage': 70, 'minimumCommonMetrics': MIN_COMMON_METRICS, 'minimumCategories': MIN_CATEGORIES, 'method': 'role-weighted-percentile-cosine70-closeness30-v3'}
