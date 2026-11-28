from __future__ import annotations
from report_module.rate_evidence import RATES, VERSION, apply_merged_rates, key as metric_key

import re
from math import isclose, isfinite
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import FOLD_CHAR_MAP_FROM, FOLD_CHAR_MAP_TO, norm_name


SEARCH_LIMIT = 20
MAX_SELECTED_ROWS = 1
_nationality_cache: List[str] | None = None


def _search_norm(value: str | None) -> str:
    folded = norm_name(value)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def _folded_column_sql(column: str) -> str:
    return (
        "BTRIM(LOWER(REGEXP_REPLACE(TRANSLATE("
        f"COALESCE({column}, ''), '{FOLD_CHAR_MAP_FROM}', '{FOLD_CHAR_MAP_TO}'"
        "), '[^a-zA-Z0-9]+', ' ', 'g')))"
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("%", ""))
        except ValueError:
            return None
    return None


def _row_key(row: Mapping[str, Any]) -> tuple[int, int, int]:
    return (int(row["team_id"]), int(row["league_id"]), int(row["season_id"]))


def _source_key(row: Mapping[str, Any]) -> str:
    return f'{int(row["team_id"])}:{int(row["league_id"])}:{int(row["season_id"])}'


def search_season_players(
    db: Session,
    query: str,
    limit: int = SEARCH_LIMIT,
    nationality: str | None = None,
) -> List[Dict[str, Any]]:
    normalized = _search_norm(query)
    if len(normalized) < 2:
        return []
    tokens = list(dict.fromkeys(normalized.split()))[:8]
    folded_name = _folded_column_sql("player_name")
    token_sql = " AND ".join(
        f"{folded_name} LIKE :token_contains_{index}"
        for index, _ in enumerate(tokens)
    )
    params: Dict[str, Any] = {
        "query_norm": normalized,
        "query_prefix": f"{normalized}%",
        "limit": max(1, min(int(limit or SEARCH_LIMIT), 50)),
        "nationality": nationality.strip() if nationality and nationality.strip() else None,
        "nationality_norm": _search_norm(nationality),
    }
    params.update({f"token_contains_{index}": f"%{token}%" for index, token in enumerate(tokens)})

    rows = db.execute(
        text(
            f"""
            WITH matched_names AS (
                SELECT
                    player_id,
                    player_name,
                    CASE
                        WHEN {folded_name} = :query_norm THEN 0
                        WHEN {folded_name} LIKE :query_prefix THEN 1
                        ELSE 2
                    END AS match_rank
                FROM player_comp_season_data
                WHERE {token_sql}
                  AND (
                    :nationality IS NULL
                    OR LOWER(COALESCE(nationality_name, '')) = LOWER(:nationality)
                    OR {_folded_column_sql("nationality_name")} = :nationality_norm
                  )
            ),
            best_aliases AS (
                SELECT DISTINCT ON (player_id)
                    player_id, player_name, match_rank
                FROM matched_names
                ORDER BY player_id, match_rank, LENGTH(player_name) DESC, player_name
            ),
            candidates AS (
                SELECT player_id, player_name, match_rank
                FROM best_aliases
                ORDER BY match_rank, player_id
                LIMIT :limit
            )
            SELECT
                candidates.player_id,
                latest.player_name AS display_name,
                candidates.player_name AS matched_alias,
                latest.nationality_name,
                latest.team_name AS latest_team_name,
                latest.position_name AS latest_position_name,
                latest.position_counts AS latest_position_counts,
                latest.age AS latest_age,
                latest.season_name AS latest_season_name,
                history.row_count,
                history.first_season_name,
                history.latest_season_name AS history_latest_season_name
            FROM candidates
            JOIN LATERAL (
                SELECT
                    player_name,
                    nationality_name,
                    team_name,
                    position_name,
                    position_counts,
                    age,
                    season_name
                FROM player_comp_season_data
                WHERE player_id = candidates.player_id
                ORDER BY season_id DESC, updated_at DESC
                LIMIT 1
            ) latest ON TRUE
            JOIN LATERAL (
                SELECT
                    COUNT(*)::int AS row_count,
                    MIN(season_name) AS first_season_name,
                    MAX(season_name) AS latest_season_name
                FROM player_comp_season_data
                WHERE player_id = candidates.player_id
            ) history ON TRUE
            ORDER BY candidates.match_rank, latest.player_name, candidates.player_id
            """
        ),
        params,
    ).mappings().all()

    return [
        {
            "playerId": int(row["player_id"]),
            "displayName": str(row["display_name"] or row["matched_alias"] or row["player_id"]),
            "matchedAlias": str(row["matched_alias"] or ""),
            "nationality": str(row["nationality_name"] or ""),
            "latestTeam": str(row["latest_team_name"] or ""),
            "latestPosition": str(row["latest_position_name"] or ""),
            "latestRole": _primary_season_role(row["latest_position_counts"]),
            "latestAge": int(row["latest_age"]) if row["latest_age"] is not None else None,
            "latestSeason": str(row["latest_season_name"] or row["history_latest_season_name"] or ""),
            "firstSeason": str(row["first_season_name"] or ""),
            "rowCount": int(row["row_count"] or 0),
        }
        for row in rows
    ]


def _primary_season_role(position_counts: Any) -> str:
    if not isinstance(position_counts, Mapping):
        return ""
    roles = [
        (count, role)
        for role, raw in position_counts.items()
        if role and (count := _number(raw)) is not None and isfinite(count) and count > 0
    ]
    return max(roles, default=(0, ""))[1]


def get_season_player_nationalities(db: Session) -> List[str]:
    global _nationality_cache
    if _nationality_cache is None:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT BTRIM(nationality_name) AS nationality
                FROM player_comp_season_data
                WHERE nationality_name IS NOT NULL
                  AND BTRIM(nationality_name) <> ''
                ORDER BY nationality
                """
            )
        ).scalars().all()
        _nationality_cache = [str(value) for value in rows]
    return list(_nationality_cache)


def _fetch_player_rows(db: Session, player_id: int) -> List[Dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                pcsd.player_id,
                team_id,
                league_id,
                season_id,
                pcsd.player_name,
                team_name,
                league_name,
                league_type,
                league_sub_type,
                league_country_name,
                league_short_code,
                league_image_path,
                season_name,
                gender,
                height,
                weight,
                age,
                match_count,
                nationality_name,
                position_name,
                position_counts,
                stats,
                pcsd.updated_at,
                epi.image_url
            FROM player_comp_season_data pcsd
            LEFT JOIN enterprise_player_images epi
              ON epi.player_id = pcsd.player_id
             AND epi.image_status = 'available'
            WHERE pcsd.player_id = :player_id
            ORDER BY pcsd.season_id DESC, pcsd.league_name, pcsd.team_name
            """
        ),
        {"player_id": int(player_id)},
    ).mappings().all()
    return [dict(row) for row in rows]


def _row_out(row: Mapping[str, Any]) -> Dict[str, Any]:
    match_count = int(row.get("match_count") or 0)
    stats = row.get("stats") or {}
    average_minutes = next((_number(value) for name, value in stats.items()
                            if metric_key(name) == "minutes_played"), None)
    return {
        "key": _source_key(row),
        "playerId": int(row["player_id"]),
        "teamId": int(row["team_id"]),
        "teamName": str(row.get("team_name") or ""),
        "leagueId": int(row["league_id"]),
        "leagueName": str(row.get("league_name") or ""),
        "leagueType": str(row.get("league_type") or ""),
        "leagueSubType": str(row.get("league_sub_type") or ""),
        "country": str(row.get("league_country_name") or ""),
        "leagueShortCode": str(row.get("league_short_code") or ""),
        "leagueImagePath": str(row.get("league_image_path") or ""),
        "seasonId": int(row["season_id"]),
        "seasonName": str(row.get("season_name") or ""),
        "matchCount": match_count,
        "minutes": _record_minutes(average_minutes, _source_counts(stats), match_count),
        "positionName": str(row.get("position_name") or ""),
        "positionCounts": dict(row.get("position_counts") or {}),
        "age": int(row["age"]) if row.get("age") is not None else None,
        "height": _number(row.get("height")),
        "weight": _number(row.get("weight")),
    }


def get_player_season_rows(db: Session, player_id: int) -> Dict[str, Any]:
    rows = _fetch_player_rows(db, player_id)
    if not rows:
        raise ValueError(f"Season player not found: {player_id}")
    latest = rows[0]
    return {
        "player": {
            "playerId": int(latest["player_id"]),
            "displayName": str(latest.get("player_name") or latest["player_id"]),
            "nationality": str(latest.get("nationality_name") or ""),
            "gender": str(latest.get("gender") or ""),
            "latestTeam": str(latest.get("team_name") or ""),
            "latestSeason": str(latest.get("season_name") or ""),
            "imageUrl": str(latest.get("image_url") or "") or None,
        },
        "rows": [_row_out(row) for row in rows],
    }


def _latest_value(rows: Iterable[Mapping[str, Any]], field: str) -> Any:
    for row in rows:
        value = row.get(field)
        if value is not None and value != "":
            return value
    return None


def _source_counts(source_stats: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = source_stats.get("_rate_inputs")
    return evidence.get("counts", {}) if isinstance(evidence, dict) and evidence.get("version") == VERSION else {}


def _record_minutes(average_minutes: float | None, counts: Mapping[str, Any], match_count: int) -> float | None:
    minutes_evidence = counts.get("minutes_played", {})
    if minutes_evidence:
        record_minutes = _number(minutes_evidence.get("sum")) if minutes_evidence.get("covered") == match_count else None
    else:
        record_minutes = average_minutes * match_count if average_minutes is not None else None
    return record_minutes if record_minutes is not None and isfinite(record_minutes) and record_minutes > 0 else None


def _metric_display_values(
    stats: Mapping[str, Any], source_stats: Mapping[str, Any], match_count: int,
) -> Dict[str, Dict[str, float | None]]:
    counts = _source_counts(source_stats)
    normalized_source = {metric_key(name): _number(value) for name, value in source_stats.items()}
    record_minutes = _record_minutes(normalized_source.get("minutes_played"), counts, match_count)
    display: Dict[str, Dict[str, float | None]] = {}
    for name, value in stats.items():
        numeric = _number(value)
        if numeric is None or not isfinite(numeric):
            continue
        key = metric_key(name)
        if key in RATES or key.endswith("_percentage") or key in {"rating", "shooting_performance"}:
            display[name] = {"total": numeric, "per90": numeric}
            continue

        observation = counts.get(key, {})
        recorded_total = _number(observation.get("sum"))
        if recorded_total is not None and not isfinite(recorded_total):
            recorded_total = None
        coverage_sensitive = key in {"expected_goals", "expected_goals_on_target"}
        if coverage_sensitive and recorded_total is None:
            display[name] = {"total": None, "per90": None}
            continue
        if recorded_total is None:
            if not observation and match_count > 0:
                # Legacy volume metrics use the existing per-appearance convention.
                total = numeric * match_count
                per90 = total * 90 / record_minutes if record_minutes is not None else None
                display[name] = {"total": total, "per90": numeric if key == "minutes_played" else per90}
            else:
                display[name] = {"total": None, "per90": None}
            continue

        covered = observation.get("covered")
        source_average = normalized_source.get(key)
        # Sparse event counts can omit zero-event matches. The stored average
        # confirms whether the recorded sum was normalized over all appearances.
        whole_record_count = not coverage_sensitive and source_average is not None and isclose(
            source_average * match_count, recorded_total, abs_tol=0.00005 * match_count,
        )
        per90 = None
        if record_minutes is not None and (covered == match_count or whole_record_count):
            per90 = recorded_total * 90 / record_minutes
        if key == "minutes_played":
            per90 = numeric
        display[name] = {"total": recorded_total, "per90": per90}
    return display


def aggregate_player_seasons(
    db: Session,
    player_id: int,
    sources: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    if not sources:
        raise ValueError("Select at least one season row")

    requested = {
        (int(source["teamId"]), int(source["leagueId"]), int(source["seasonId"]))
        for source in sources
    }
    if len(requested) != len(sources):
        raise ValueError("Duplicate season rows are not allowed")
    if len(sources) > MAX_SELECTED_ROWS:
        raise ValueError("Only one season row may be selected")

    all_rows = _fetch_player_rows(db, player_id)
    selected = [row for row in all_rows if _row_key(row) in requested]
    if len(selected) != len(requested):
        raise ValueError("One or more selected season rows were not found for this player")

    selected.sort(key=lambda row: (int(row["season_id"]), row.get("updated_at")), reverse=True)
    total_matches = sum(max(int(row.get("match_count") or 0), 0) for row in selected)
    position_counts: Dict[str, float] = {}
    metric_totals: Dict[str, float] = {}
    metric_weights: Dict[str, float] = {}

    for row in selected:
        weight = float(max(int(row.get("match_count") or 0), 0))
        for position, count in (row.get("position_counts") or {}).items():
            numeric = _number(count)
            if numeric is not None:
                position_counts[str(position)] = position_counts.get(str(position), 0.0) + numeric
        for metric, value in (row.get("stats") or {}).items():
            numeric = _number(value)
            if numeric is None:
                continue
            metric_totals[str(metric)] = metric_totals.get(str(metric), 0.0) + numeric * weight
            metric_weights[str(metric)] = metric_weights.get(str(metric), 0.0) + weight

    stats = {
        metric: round(total / metric_weights[metric], 4)
        for metric, total in metric_totals.items()
        if metric_weights.get(metric, 0) > 0
    }
    for row in selected:
        for metric, value in (row.get("stats") or {}).items():
            if value is None:
                stats.setdefault(metric, None)
    apply_merged_rates(stats, [row.get("stats") or {} for row in selected],
                       [max(int(row.get("match_count") or 0), 0) for row in selected])
    seasons = list(dict.fromkeys(str(row.get("season_name") or "") for row in selected if row.get("season_name")))
    teams = list(dict.fromkeys(str(row.get("team_name") or "") for row in selected if row.get("team_name")))
    competitions = list(dict.fromkeys(str(row.get("league_name") or "") for row in selected if row.get("league_name")))
    latest = selected[0]

    return {
        "playerId": int(player_id),
        "displayName": str(latest.get("player_name") or player_id),
        "nationality": str(_latest_value(selected, "nationality_name") or ""),
        "gender": str(_latest_value(selected, "gender") or ""),
        "age": int(_latest_value(selected, "age")) if _latest_value(selected, "age") is not None else None,
        "height": _number(_latest_value(selected, "height")),
        "weight": _number(_latest_value(selected, "weight")),
        "selectedRowCount": len(selected),
        "matchCount": total_matches,
        "seasons": seasons,
        "teams": teams,
        "competitions": competitions,
        "positionCounts": {key: round(value, 2) for key, value in position_counts.items()},
        "stats": {name: value for name, value in stats.items() if _number(value) is not None},
        "metricDisplay": _metric_display_values(stats, latest.get("stats") or {}, total_matches),
        "selectedRows": [_row_out(row) for row in selected],
    }
