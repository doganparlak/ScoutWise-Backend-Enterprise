from __future__ import annotations

from typing import Any, Dict, List
import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import player_pool_table


DEFAULT_LIMIT = 10


def _metadata_value(metadata: Dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _normalize_position_counts(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}

    counts: Dict[str, int] = {}
    for key, count in value.items():
        if key is None:
            continue
        code = str(key).strip()
        if not code:
            continue
        try:
            numeric_count = int(count)
        except (TypeError, ValueError):
            continue
        if numeric_count > 0:
            counts[code] = numeric_count

    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _with_position_distribution(row: Dict[str, Any]) -> Dict[str, Any]:
    content = dict(row.get("content") or {})
    position_counts = _normalize_position_counts(content.get("position_counts"))

    raw_seen = content.get("position_names_seen")
    if isinstance(raw_seen, list):
        position_names_seen = []
        for value in raw_seen:
            code = str(value or "").strip()
            if code and code not in position_names_seen:
                position_names_seen.append(code)
    else:
        position_names_seen = []

    if not position_names_seen:
        position_names_seen = list(position_counts.keys())

    content["position_counts"] = position_counts
    content["position_count_total"] = sum(position_counts.values())
    content["position_names_seen"] = position_names_seen
    content["primary_position_code"] = (
        str(content.get("primary_position_code") or "").strip()
        or (position_names_seen[0] if position_names_seen else None)
    )
    return {**row, "content": content}


def _cached_score(metadata: Dict[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        score = max(0, min(100, int(value)))
        return score if score > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            score = max(0, min(100, int(float(stripped))))
        except ValueError:
            return None
        return score if score > 0 else None
    return None


def _player_id_for_scoring(player_id: Any) -> int | str | None:
    try:
        return int(str(player_id))
    except (TypeError, ValueError):
        return None


def _ensure_weekly_scores(
    db: Session,
    row: Dict[str, Any],
    world_cup_mode: bool,
) -> Dict[str, Any]:
    content = dict(row.get("content") or {})
    player_id = _player_id_for_scoring(row.get("id"))
    if player_id is None:
        return row

    needs_form = _cached_score(content, "form") is None
    needs_potential = not world_cup_mode and _cached_score(content, "potential") is None
    if not needs_form and not needs_potential:
        return row

    from potential_form_module.form import reveal_player_form
    from potential_form_module.potential import reveal_player_potential

    if needs_potential:
        try:
            potential_result = reveal_player_potential(db, player_id, world_cup_mode)
            potential = potential_result.get("potential")
            if potential is not None:
                content["potential"] = potential
        except Exception:
            pass

    if needs_form:
        try:
            form_result = reveal_player_form(db, player_id, world_cup_mode)
            form = form_result.get("form")
            if form is not None:
                content["form"] = form
        except Exception:
            pass

    return {**row, "content": content}


def _world_cup_player_key(metadata: Dict[str, Any]) -> str:
    parts = [
        _metadata_value(metadata, "player_name").lower(),
        _metadata_value(metadata, "gender").lower(),
        _metadata_value(metadata, "age"),
        _metadata_value(metadata, "height"),
        _metadata_value(metadata, "weight"),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _record_world_cup_player_search(db: Session, player_id_int: int) -> None:
    player = db.execute(
        text("""
        SELECT id, metadata
        FROM player_data_wc
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id_int},
    ).mappings().first()
    if not player:
        return

    metadata = dict(player["metadata"] or {})
    db.execute(
        text("""
        INSERT INTO player_pool_world_cup_searches (
            player_key,
            current_player_id,
            player_name,
            gender,
            age,
            height,
            weight,
            team,
            player_metadata,
            search_count,
            last_searched_at
        )
        VALUES (
            :player_key,
            :current_player_id,
            :player_name,
            :gender,
            :age,
            :height,
            :weight,
            :team,
            CAST(:player_metadata AS jsonb),
            1,
            NOW()
        )
        ON CONFLICT (player_key) DO UPDATE
        SET current_player_id = EXCLUDED.current_player_id,
            player_name = EXCLUDED.player_name,
            gender = EXCLUDED.gender,
            age = EXCLUDED.age,
            height = EXCLUDED.height,
            weight = EXCLUDED.weight,
            team = EXCLUDED.team,
            player_metadata = EXCLUDED.player_metadata,
            search_count = player_pool_world_cup_searches.search_count + 1,
            last_searched_at = NOW()
        """),
        {
            "player_key": _world_cup_player_key(metadata),
            "current_player_id": player_id_int,
            "player_name": _metadata_value(metadata, "player_name"),
            "gender": _metadata_value(metadata, "gender"),
            "age": _metadata_value(metadata, "age"),
            "height": _metadata_value(metadata, "height"),
            "weight": _metadata_value(metadata, "weight"),
            "team": _metadata_value(metadata, "team"),
            "player_metadata": json.dumps(metadata),
        },
    )


def record_player_search(db: Session, player_id: str, world_cup_mode: bool = False) -> None:
    player_id_int = int(player_id)
    if world_cup_mode:
        _record_world_cup_player_search(db, player_id_int)
        return

    table_name = player_pool_table(False)
    player_exists = db.execute(
        text(f"""
        SELECT id
        FROM {table_name}
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id_int},
    ).first()
    if not player_exists:
        return

    db.execute(
        text("""
        INSERT INTO player_pool_weekly_searches (
            week_start,
            player_id,
            search_count,
            last_searched_at
        )
        VALUES (DATE_TRUNC('week', NOW())::date, :player_id, 1, NOW())
        ON CONFLICT (week_start, player_id) DO UPDATE
        SET search_count = player_pool_weekly_searches.search_count + 1,
            last_searched_at = NOW()
        """),
        {"player_id": player_id_int},
    )


def get_weekly_popular_players(
    db: Session,
    limit: int = DEFAULT_LIMIT,
    world_cup_mode: bool = False,
) -> List[Dict[str, Any]]:
    if world_cup_mode:
        rows = db.execute(
            text("""
            SELECT
                COALESCE(pd.id::text, wc.current_player_id::text, wc.player_key) AS id,
                COALESCE(pd.metadata, wc.player_metadata) AS content
            FROM player_pool_world_cup_searches wc
            LEFT JOIN LATERAL (
                SELECT id, metadata
                FROM player_data_wc pd
                WHERE LOWER(TRIM(pd.metadata->>'player_name')) = LOWER(TRIM(wc.player_name))
                  AND COALESCE(TRIM(pd.metadata->>'gender'), '') = COALESCE(TRIM(wc.gender), '')
                  AND COALESCE(TRIM(pd.metadata->>'age'), '') = COALESCE(TRIM(wc.age), '')
                  AND COALESCE(TRIM(pd.metadata->>'height'), '') = COALESCE(TRIM(wc.height), '')
                  AND COALESCE(TRIM(pd.metadata->>'weight'), '') = COALESCE(TRIM(wc.weight), '')
                ORDER BY CASE WHEN pd.id = wc.current_player_id THEN 0 ELSE 1 END, pd.id DESC
                LIMIT 1
            ) pd ON TRUE
            ORDER BY wc.search_count DESC, wc.last_searched_at DESC, wc.player_name ASC
            LIMIT :limit
            """),
            {"limit": int(limit or DEFAULT_LIMIT)},
        ).mappings().all()
        return [
            _ensure_weekly_scores(
                db,
                _with_position_distribution({"id": row["id"], "content": row["content"] or {}}),
                True,
            )
            for row in rows
        ]

    table_name = player_pool_table(False)
    rows = db.execute(
        text(f"""
        SELECT
            pd.id,
            pd.metadata AS content
        FROM player_pool_weekly_searches pws
        JOIN {table_name} pd ON pd.id = pws.player_id
        WHERE pws.week_start = DATE_TRUNC('week', NOW())::date
        ORDER BY pws.search_count DESC, pws.last_searched_at DESC, pd.id DESC
        LIMIT :limit
        """),
        {"limit": int(limit or DEFAULT_LIMIT)},
    ).mappings().all()
    return [
        _ensure_weekly_scores(
            db,
            _with_position_distribution({"id": row["id"], "content": row["content"] or {}}),
            False,
        )
        for row in rows
    ]
