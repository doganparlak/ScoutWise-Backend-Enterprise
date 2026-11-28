"""Deterministic role-filtered player similarity; no LLM or tactical inputs."""
from collections import OrderedDict
from hashlib import sha256
import json
from threading import Lock

from bisect import bisect_left, bisect_right
from math import sqrt
from heapq import nsmallest
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from matchup_module.comparison import _fetch_player_metadata
from player_pool_module.player_pool import search_players, fetch_player_rows_by_ids
from scoutwise_pro_module.discovery import ALIASES, BUNDLES, CONTEXT_METRICS, CATEGORY_DEFINITIONS, CONTEXT_BUNDLES, DiscoveryFilters, player_metrics, discovery_roles, discovery_search_roles

from scoutwise_pro_module.similarity_weights import feature_weights

MIN_COVERAGE = 0.70
MIN_COMMON_METRICS = 15
MIN_CATEGORIES = 3
MAX_CACHED_RANKINGS = 128
_rank_cache = OrderedDict()
_revision_enabled = set()
_rank_cache_lock = Lock()

METRIC_CATEGORIES = {
    metric.lstrip('-'): category
    for category, _, _, groups in CATEGORY_DEFINITIONS
    for key, _, _, metrics in groups
    for metric in [*metrics, *CONTEXT_BUNDLES.get(f'{category}.{key}', [])]
}


# Keep every input to role eligibility, metric normalization, data sufficiency
# and duplicate selection. Do not restrict this to the source's shared metrics:
# candidate completeness determines which duplicate record wins.
SIMILARITY_METADATA_FIELDS = sorted({
    'player_id', 'position_counts', 'primary_position_code', 'Minutes Played', 'match_count',
    *(metric.lstrip('-') for metrics in BUNDLES.values() for metric in metrics),
    *CONTEXT_METRICS,
    *(alias for aliases in ALIASES.values() for alias in aliases),
})


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
    preview: bool = False


def identity(row):
    return str(row['content'].get('player_id') or f"row:{row['id']}")


def rank_similar(source_row, rows):
    source = player_metrics(source_row['content'], include_context=True)
    role_shares = discovery_roles(source_row['content'])
    # Expand only roles that passed the existing primary/secondary-role rule.
    # Keep the source shares unchanged for similarity feature weighting.
    roles = discovery_search_roles(role_shares)
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
    def scored_candidates():
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
            yield {'player': entry['row'], 'similarity': score, 'coverage': round(100 * coverage, 1), 'commonMetricCount': len(common), 'matchedRoles': entry['roles']}
    # Consume every score, retaining at most 50 ranking entries. nsmallest
    # preserves the previous stable ordering, including exact key ties.
    return nsmallest(50, scored_candidates(), key=lambda row: (-row['similarity'], -row['coverage'], int(row['player']['id'])))


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


def _player_data_revision(db):
    bind = db.get_bind()
    with _rank_cache_lock:
        enabled = bind in _revision_enabled
    if not enabled:
        # Use the caller's connection so a burst of requests cannot exhaust
        # the pool by holding one connection while waiting for another.
        if db.execute(text("SELECT to_regclass('public.scoutwise_player_data_revision')")).scalar() is None:
            return None
        with _rank_cache_lock:
            _revision_enabled.add(bind)
    return db.execute(text("SELECT revision FROM public.scoutwise_player_data_revision WHERE singleton")).scalar_one()


def _profile_fingerprint(content):
    inputs = {key: content[key] for key in SIMILARITY_METADATA_FIELDS if key in content}
    return sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).digest()


def _rank_candidates(db, source, filters):
    candidate_filters = dict(filters)
    choices = {key: candidate_filters.pop(key, []) for key in ('nationality', 'team', 'league')}
    rows = search_players(
        db, candidate_filters,
        all_matches=True,
        candidate_roles=sorted(discovery_search_roles(discovery_roles(source['content']))),
        candidate_choices=choices, metadata_fields=SIMILARITY_METADATA_FIELDS, batch_size=1000,
    )
    return [
        (int(match['player']['id']), _profile_fingerprint(match['player']['content']),
         {key: value for key, value in match.items() if key != 'player'})
        for match in rank_similar(source, rows)
    ]


def similar_players(db, payload):
    revision = _player_data_revision(db)
    source = load_similarity_source(db, payload.playerId)
    if not source_is_eligible(source):
        raise HTTPException(status_code=422, detail='similarity_source_insufficient')

    filters = payload.filters.model_dump(mode='json', exclude_none=True)
    cache_key = (db.get_bind(), revision, payload.playerId, json.dumps(filters, sort_keys=True))
    ranked = None
    if revision is not None:
        with _rank_cache_lock:
            ranked = _rank_cache.get(cache_key)
            if ranked is not None:
                _rank_cache.move_to_end(cache_key)
    cache_miss = ranked is None
    if cache_miss:
        ranked = _rank_candidates(db, source, filters)

    selected = ranked[:3] if payload.preview else ranked
    profiles = {int(row['id']): row for row in fetch_player_rows_by_ids(db, [row_id for row_id, _, _ in selected])}
    results = []
    for row_id, fingerprint, score in selected:
        full = profiles.get(row_id)
        if full is None or _profile_fingerprint(full['content']) != fingerprint:
            raise HTTPException(status_code=409, detail='Player data changed during search. Please retry.')
        results.append({**score, 'player': full})

    if revision is not None:
        if _player_data_revision(db) != revision:
            raise HTTPException(status_code=409, detail='Player data changed during search. Please retry.')
        if cache_miss:
            with _rank_cache_lock:
                _rank_cache[cache_key] = ranked
                _rank_cache.move_to_end(cache_key)
                if len(_rank_cache) > MAX_CACHED_RANKINGS:
                    _rank_cache.popitem(last=False)
    return {'players': results, 'minimumCoverage': 70, 'minimumCommonMetrics': MIN_COMMON_METRICS, 'minimumCategories': MIN_CATEGORIES, 'method': 'role-weighted-percentile-cosine70-closeness30-v3'}
