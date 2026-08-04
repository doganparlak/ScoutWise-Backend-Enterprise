from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import player_pool_table


def _fetch_player_metadata(db: Session, player_id: str, world_cup_mode: bool = False) -> Dict[str, Any]:
    try:
        player_id_int = int(player_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid player_id") from exc

    table_name = player_pool_table(world_cup_mode)
    row = db.execute(
        text(f"""
        SELECT id, metadata AS content
        FROM {table_name}
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id_int},
    ).mappings().first()

    if not row:
        raise ValueError(f"Player not found: {player_id}")

    return {
        "id": row["id"],
        "content": row["content"] or {},
    }


def _source_key(team_id: Any, competition_id: Any) -> str:
    return json.dumps([str(team_id or ""), str(competition_id or "")], separators=(",", ":"))


def _fetch_comp_rows(db: Session, player_id: str) -> List[Dict[str, Any]]:
    player = _fetch_player_metadata(db, player_id, False)
    metadata = player["content"] or {}
    player_name = str(metadata.get("player_name") or metadata.get("name") or "").strip()
    nationality = str(
        metadata.get("nationality_name") or metadata.get("nationality") or ""
    ).strip()
    if not player_name:
        raise ValueError(f"Player identity not found: {player_id}")
    rows = db.execute(
        text("""
            SELECT
                player_id,
                player_name,
                nationality_name,
                age,
                height,
                weight,
                team_id,
                team_name,
                league_id,
                league_name,
                league_short_code,
                league_country_name,
                match_count,
                position_counts,
                stats
            FROM player_comp_data
            WHERE LOWER(TRIM(player_name)) = LOWER(TRIM(:player_name))
              AND (
                :nationality = ''
                OR LOWER(TRIM(COALESCE(nationality_name, ''))) = LOWER(TRIM(:nationality))
              )
            ORDER BY team_name, league_name
        """),
        {"player_name": player_name, "nationality": nationality},
    ).mappings().all()
    return [dict(row) for row in rows]


def get_player_comparison_sources(db: Session, player_id: str) -> List[Dict[str, Any]]:
    rows = _fetch_comp_rows(db, player_id)
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = _source_key(row.get("team_id"), row.get("league_id"))
        item = grouped.setdefault(
            key,
            {
                "key": key,
                "country": str(row.get("league_country_name") or ""),
                "leagueShortCode": str(row.get("league_short_code") or ""),
                "team": str(row.get("team_name") or ""),
                "competition": str(row.get("league_name") or ""),
                "matchCount": 0.0,
            },
        )
        try:
            item["matchCount"] += float(row.get("match_count") or 0)
        except (TypeError, ValueError):
            pass
    return list(grouped.values())


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("%", ""))
        except ValueError:
            return None
    return None


def _selected_comp_metadata(
    db: Session,
    player_id: str,
    selected_sources: List[str],
    base_content: Dict[str, Any],
) -> Dict[str, Any]:
    selected = set(selected_sources)
    rows = [
        row
        for row in _fetch_comp_rows(db, player_id)
        if _source_key(row.get("team_id"), row.get("league_id")) in selected
    ]
    if not rows:
        raise ValueError(f"Selected player competition data not found: {player_id}")

    result: Dict[str, Any] = {
        key: base_content.get(key)
        for key in (
            "potential",
            "form",
            "gender",
            "primary_position_code",
        )
        if base_content.get(key) is not None
    }
    first = rows[0]
    result.update(
        {
            "player_name": first.get("player_name") or base_content.get("player_name"),
            "nationality_name": first.get("nationality_name") or base_content.get("nationality_name"),
            "team_name": ", ".join(dict.fromkeys(str(row.get("team_name") or "") for row in rows if row.get("team_name"))),
            "league_name": ", ".join(dict.fromkeys(str(row.get("league_name") or "") for row in rows if row.get("league_name"))),
        }
    )

    total_matches = sum(_number(row.get("match_count")) or 0 for row in rows)
    result["match_count"] = total_matches
    for field in ("age", "height", "weight"):
        weighted = [
            (_number(row.get(field)), _number(row.get("match_count")) or 1)
            for row in rows
        ]
        weighted = [(value, weight) for value, weight in weighted if value is not None]
        if weighted:
            result[field] = round(
                sum(value * weight for value, weight in weighted) / sum(weight for _, weight in weighted),
                1,
            )

    positions: Dict[str, float] = {}
    metric_totals: Dict[str, float] = {}
    metric_weights: Dict[str, float] = {}
    for row in rows:
        for role, count in (row.get("position_counts") or {}).items():
            numeric = _number(count)
            if numeric is not None:
                positions[str(role)] = positions.get(str(role), 0) + numeric
        weight = _number(row.get("match_count")) or 1
        for metric, value in (row.get("stats") or {}).items():
            numeric = _number(value)
            if numeric is None:
                continue
            metric_totals[str(metric)] = metric_totals.get(str(metric), 0) + numeric * weight
            metric_weights[str(metric)] = metric_weights.get(str(metric), 0) + weight

    result["position_counts"] = {key: round(value, 2) for key, value in positions.items()}
    result["position_names_seen"] = list(positions)
    result["position_count_total"] = round(sum(positions.values()), 2)
    result.update(
        {
            metric: round(total / metric_weights[metric], 4)
            for metric, total in metric_totals.items()
            if metric_weights.get(metric)
        }
    )
    return result


def get_matchup_comparison(
    db: Session,
    player1_id: str,
    player2_id: str,
    world_cup_mode: bool = False,
    player1_sources: List[str] | None = None,
    player2_sources: List[str] | None = None,
) -> Dict[str, Any]:
    player1 = _fetch_player_metadata(db, player1_id, world_cup_mode)
    player2 = _fetch_player_metadata(db, player2_id, world_cup_mode)
    if player1_sources:
        player1["content"] = _selected_comp_metadata(
            db, player1_id, player1_sources, player1["content"]
        )
    if player2_sources:
        player2["content"] = _selected_comp_metadata(
            db, player2_id, player2_sources, player2["content"]
        )
    return {
        "player1": player1,
        "player2": player2,
    }
