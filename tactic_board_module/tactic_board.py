from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from api_module.models import EnterpriseTacticBoardIn, EnterpriseTacticBoardOut


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def tactic_board_out(row: Any) -> EnterpriseTacticBoardOut:
    data = row._mapping if hasattr(row, "_mapping") else row
    board_data = _json_value(data.get("board_data"), {})
    if not isinstance(board_data, dict):
        board_data = {}
    created_at = data.get("created_at")
    updated_at = data.get("updated_at")
    return EnterpriseTacticBoardOut(
        id=str(data["id"]),
        name=data["name"],
        formation=data.get("formation") or board_data.get("formation") or "4-3-3",
        board_data=board_data,
        createdAt=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        updatedAt=updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
    )


def _board_json(payload: EnterpriseTacticBoardIn) -> str:
    return json.dumps(payload.board_data or {}, ensure_ascii=False)


def _is_duplicate_name_error(exc: Exception) -> bool:
    error_text = str(exc).lower()
    return "enterprise_tactic_boards_user_name_unique" in error_text or "duplicate key value" in error_text


def list_tactic_boards(db: Session, user_id: str) -> list[EnterpriseTacticBoardOut]:
    rows = db.execute(
        text("""
        SELECT id, name, formation, board_data, created_at, updated_at
        FROM enterprise_tactic_boards
        WHERE user_id = :user_id
        ORDER BY updated_at DESC
        """),
        {"user_id": user_id},
    ).mappings().all()
    return [tactic_board_out(row) for row in rows]


def create_tactic_board(db: Session, user_id: str, payload: EnterpriseTacticBoardIn) -> EnterpriseTacticBoardOut:
    board_id = str(uuid.uuid4())
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Board name is required")
    try:
        db.execute(
            text("""
            INSERT INTO enterprise_tactic_boards (id, user_id, name, formation, board_data)
            VALUES (:id, :user_id, :name, :formation, CAST(:board_data AS jsonb))
            """),
            {
                "id": board_id,
                "user_id": user_id,
                "name": name,
                "formation": payload.formation,
                "board_data": _board_json(payload),
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if _is_duplicate_name_error(exc):
            raise HTTPException(status_code=409, detail="Board name already exists") from exc
        raise

    row = db.execute(
        text("""
        SELECT id, name, formation, board_data, created_at, updated_at
        FROM enterprise_tactic_boards
        WHERE id = :id AND user_id = :user_id
        """),
        {"id": board_id, "user_id": user_id},
    ).mappings().first()
    return tactic_board_out(row)


def update_tactic_board(db: Session, user_id: str, board_id: str, payload: EnterpriseTacticBoardIn) -> EnterpriseTacticBoardOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Board name is required")
    try:
        result = db.execute(
            text("""
            UPDATE enterprise_tactic_boards
            SET name = :name,
                formation = :formation,
                board_data = CAST(:board_data AS jsonb)
            WHERE id = :id AND user_id = :user_id
            """),
            {
                "id": board_id,
                "user_id": user_id,
                "name": name,
                "formation": payload.formation,
                "board_data": _board_json(payload),
            },
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Board not found")
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        if _is_duplicate_name_error(exc):
            raise HTTPException(status_code=409, detail="Board name already exists") from exc
        raise

    row = db.execute(
        text("""
        SELECT id, name, formation, board_data, created_at, updated_at
        FROM enterprise_tactic_boards
        WHERE id = :id AND user_id = :user_id
        """),
        {"id": board_id, "user_id": user_id},
    ).mappings().first()
    return tactic_board_out(row)


def delete_tactic_board(db: Session, user_id: str, board_id: str) -> Response:
    db.execute(
        text("""
        DELETE FROM enterprise_tactic_boards
        WHERE id = :id AND user_id = :user_id
        """),
        {"id": board_id, "user_id": user_id},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
