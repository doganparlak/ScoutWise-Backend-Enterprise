from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from api_module.models import EnterpriseLineupIn, EnterpriseLineupOut


def lineup_out(row: Any) -> EnterpriseLineupOut:
    data = row._mapping if hasattr(row, "_mapping") else row
    slots_raw = data.get("slots") or []
    if isinstance(slots_raw, str):
        try:
            slots_raw = json.loads(slots_raw)
        except json.JSONDecodeError:
            slots_raw = []
    if not isinstance(slots_raw, list):
        slots_raw = []
    created_at = data.get("created_at")
    updated_at = data.get("updated_at")
    return EnterpriseLineupOut(
        id=str(data["id"]),
        name=data["name"],
        formation=data["formation"],
        teamRating=float(data.get("team_rating") or 0),
        slots=slots_raw,
        createdAt=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        updatedAt=updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
    )


def _slot_json(payload: EnterpriseLineupIn) -> str:
    return json.dumps([slot.model_dump() for slot in payload.slots], ensure_ascii=False)


def _is_duplicate_name_error(exc: Exception) -> bool:
    error_text = str(exc).lower()
    return "enterprise_lineups_user_id_name_key_key" in error_text or "duplicate key value" in error_text


def list_lineups(db: Session, user_id: str) -> list[EnterpriseLineupOut]:
    rows = db.execute(
        text("""
        SELECT id, name, formation, team_rating, slots, created_at, updated_at
        FROM enterprise_lineups
        WHERE user_id = :user_id
        ORDER BY updated_at DESC
        """),
        {"user_id": user_id},
    ).mappings().all()
    return [lineup_out(row) for row in rows]


def create_lineup(db: Session, user_id: str, payload: EnterpriseLineupIn) -> EnterpriseLineupOut:
    lineup_id = str(uuid.uuid4())
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Lineup name is required")
    try:
        db.execute(
            text("""
            INSERT INTO enterprise_lineups (
                id, user_id, name, formation, team_rating, slots
            )
            VALUES (
                :id, :user_id, :name, :formation, :team_rating, CAST(:slots AS jsonb)
            )
            """),
            {
                "id": lineup_id,
                "user_id": user_id,
                "name": name,
                "formation": payload.formation,
                "team_rating": payload.teamRating,
                "slots": _slot_json(payload),
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if _is_duplicate_name_error(exc):
            raise HTTPException(status_code=409, detail="Lineup name already exists") from exc
        raise

    row = db.execute(
        text("""
        SELECT id, name, formation, team_rating, slots, created_at, updated_at
        FROM enterprise_lineups
        WHERE id = :id
          AND user_id = :user_id
        """),
        {"id": lineup_id, "user_id": user_id},
    ).mappings().first()
    return lineup_out(row)


def update_lineup(db: Session, user_id: str, lineup_id: str, payload: EnterpriseLineupIn) -> EnterpriseLineupOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Lineup name is required")
    try:
        result = db.execute(
            text("""
            UPDATE enterprise_lineups
            SET name = :name,
                formation = :formation,
                team_rating = :team_rating,
                slots = CAST(:slots AS jsonb)
            WHERE id = :id
              AND user_id = :user_id
            """),
            {
                "id": lineup_id,
                "user_id": user_id,
                "name": name,
                "formation": payload.formation,
                "team_rating": payload.teamRating,
                "slots": _slot_json(payload),
            },
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Lineup not found")
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        if _is_duplicate_name_error(exc):
            raise HTTPException(status_code=409, detail="Lineup name already exists") from exc
        raise

    row = db.execute(
        text("""
        SELECT id, name, formation, team_rating, slots, created_at, updated_at
        FROM enterprise_lineups
        WHERE id = :id
          AND user_id = :user_id
        """),
        {"id": lineup_id, "user_id": user_id},
    ).mappings().first()
    return lineup_out(row)


def delete_lineup(db: Session, user_id: str, lineup_id: str) -> Response:
    db.execute(
        text("""
        DELETE FROM enterprise_lineups
        WHERE id = :id
          AND user_id = :user_id
        """),
        {"id": lineup_id, "user_id": user_id},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
