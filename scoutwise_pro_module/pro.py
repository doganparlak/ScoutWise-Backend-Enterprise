from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from api_module.models import EnterpriseProChatIn, EnterpriseProStrategyIn, EnterpriseProStrategyOut, EnterpriseProStrategySavedIn, EnterpriseProStrategySavedOut
from api_module.utilities import (
    delete_chat_messages,
    normalize_lang,
    session_exists_and_active,
    split_response_parts,
)
from chatbot_module.chatbot_agentic import answer_question


def _session_log_label(token: str) -> str:
    parts = token.split(":")
    if len(parts) >= 3:
        return f"{parts[0]}:*:{parts[-1]}"
    return token[-32:]


def _message_preview(message: str, limit: int = 140) -> str:
    compact = " ".join((message or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def user_session_token(user_id: str, session_id: str | None) -> str:
    client_session = (session_id or "default").strip() or "default"
    return f"enterprise:{user_id}:{client_session}"


def get_strategy(db: Session, user_id: str) -> EnterpriseProStrategyOut:
    row = db.execute(
        text(
            """
            SELECT strategy, updated_at
            FROM enterprise_pro_active_strategies
            WHERE user_id = :user_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()
    if not row:
        return EnterpriseProStrategyOut(strategy="", updatedAt=None)
    return EnterpriseProStrategyOut(
        strategy=row["strategy"] or "",
        updatedAt=row["updated_at"].isoformat() if row["updated_at"] else None,
    )


def save_strategy(db: Session, user_id: str, payload: EnterpriseProStrategyIn) -> EnterpriseProStrategyOut:
    row = db.execute(
        text(
            """
            INSERT INTO enterprise_pro_active_strategies (user_id, strategy, created_at, updated_at)
            VALUES (:user_id, :strategy, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET strategy = EXCLUDED.strategy,
                updated_at = NOW()
            RETURNING strategy, updated_at
            """
        ),
        {"user_id": user_id, "strategy": payload.strategy.strip()},
    ).mappings().first()
    db.commit()
    return EnterpriseProStrategyOut(
        strategy=row["strategy"] or "",
        updatedAt=row["updated_at"].isoformat() if row["updated_at"] else None,
    )


def _strategy_saved_out(row: Dict[str, Any]) -> EnterpriseProStrategySavedOut:
    return EnterpriseProStrategySavedOut(
        id=str(row["id"]),
        strategyName=row["strategy_name"] or "Default Strategy",
        strategy=row["strategy"] or "",
        createdAt=row["created_at"].isoformat() if row["created_at"] else "",
        updatedAt=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


def list_strategies(db: Session, user_id: str) -> list[EnterpriseProStrategySavedOut]:
    rows = db.execute(
        text(
            """
            SELECT id, strategy_name, strategy, created_at, updated_at
            FROM enterprise_pro_strategies
            WHERE user_id = :user_id
            ORDER BY updated_at DESC
            """
        ),
        {"user_id": user_id},
    ).mappings().all()
    return [_strategy_saved_out(row) for row in rows]


def save_named_strategy(db: Session, user_id: str, payload: EnterpriseProStrategySavedIn) -> EnterpriseProStrategySavedOut:
    row = db.execute(
        text(
            """
            INSERT INTO enterprise_pro_strategies (user_id, strategy_name, strategy, created_at, updated_at)
            VALUES (:user_id, :strategy_name, :strategy, NOW(), NOW())
            ON CONFLICT (user_id, lower(strategy_name)) DO UPDATE
            SET strategy = EXCLUDED.strategy,
                updated_at = NOW()
            RETURNING id, strategy_name, strategy, created_at, updated_at
            """
        ),
        {
            "user_id": user_id,
            "strategy_name": payload.strategyName.strip() or "Default Strategy",
            "strategy": payload.strategy.strip(),
        },
    ).mappings().first()
    db.commit()
    return _strategy_saved_out(row)


def delete_named_strategy(db: Session, user_id: str, strategy_id: str) -> None:
    result = db.execute(
        text(
            """
            DELETE FROM enterprise_pro_strategies
            WHERE user_id = :user_id AND id = :strategy_id
            """
        ),
        {"user_id": user_id, "strategy_id": strategy_id},
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Strategy not found")


def ensure_chat_session(db: Session, *, token: str, user_id: str, lang: str) -> None:
    if session_exists_and_active(db, token):
        db.execute(
            text(
                """
                UPDATE enterprise_pro_chat_sessions
                SET language = :lang,
                    updated_at = NOW()
                WHERE token = :token
                  AND ended_at IS NULL
                """
            ),
            {"token": token, "lang": lang},
        )
    else:
        db.execute(
            text(
                """
                INSERT INTO enterprise_pro_chat_sessions (token, user_id, language, created_at, updated_at, ended_at)
                VALUES (:token, :user_id, :lang, NOW(), NOW(), NULL)
                ON CONFLICT (token) DO UPDATE
                SET user_id = EXCLUDED.user_id,
                    language = EXCLUDED.language,
                    updated_at = NOW(),
                    ended_at = NULL
                """
            ),
            {"token": token, "user_id": user_id, "lang": lang},
        )
    db.commit()


def reset_chat_session(db: Session, *, token: str) -> Dict[str, Any]:
    delete_chat_messages(db, token)
    db.execute(
        text(
            """
            UPDATE enterprise_pro_chat_sessions
            SET ended_at = NOW(),
                updated_at = NOW()
            WHERE token = :token
              AND ended_at IS NULL
            """
        ),
        {"token": token},
    )
    db.commit()
    return {"ok": True, "session_id": token, "reset": True}


def send_chat(
    db: Session,
    *,
    user_id: str,
    payload: EnterpriseProChatIn,
    accept_language: str | None,
) -> Dict[str, Any]:
    session_token = user_session_token(user_id, payload.session_id)
    lang = normalize_lang(accept_language) or "en"
    ensure_chat_session(db, token=session_token, user_id=user_id, lang=lang)

    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    strategy_text = (payload.strategy or "").strip()
    print(
        "[enterprise_pro_chat] "
        f"event=request session={_session_log_label(session_token)} "
        f"lang={lang} strategy_chars={len(strategy_text)} "
        f"message={_message_preview(message)!r}",
        flush=True,
    )
    result = answer_question(
        message,
        session_id=session_token,
        strategy=strategy_text or None,
    )
    answer_text = (result.get("answer") or "").strip()
    data = result.get("data") or {"players": []}
    players = data.get("players") if isinstance(data, dict) else []
    print(
        "[enterprise_pro_chat] "
        f"event=response session={_session_log_label(session_token)} "
        f"players={len(players or [])} response_chars={len(answer_text)}",
        flush=True,
    )
    return {
        "response": answer_text,
        "data": data,
        "response_parts": split_response_parts(answer_text),
        "timing": result.get("timing"),
    }
