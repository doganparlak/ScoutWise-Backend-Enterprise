from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek
from sqlalchemy import text

from report_module.prompts import report_system_prompt
from report_module.utilities import _first_non_empty, _normalize_roles, _score_candidate, norm_name

load_dotenv()

CHAT_LLM = ChatDeepSeek(model="deepseek-chat", temperature=0.3)

_report_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", report_system_prompt),
        ("human", "lang: {lang}\n\n{input_text}"),
    ]
)

report_chain = _report_prompt | CHAT_LLM | StrOutputParser()


def fetch_docs_for_favorite(
    db,
    player_identity: Dict[str, Any],
    limit_docs: int = 30,
) -> List[Dict[str, Any]]:
    club_player_id = player_identity.get("club_player_id") or player_identity.get("clubPlayerId")
    if club_player_id is not None:
        row = db.execute(
            text(
                """
                SELECT id, metadata, content
                FROM player_data
                WHERE id = :player_id
                LIMIT 1
                """
            ),
            {"player_id": club_player_id},
        ).mappings().first()
        if row:
            return [{"id": row["id"], "content": row.get("content"), "metadata": row.get("metadata")}]

    name = player_identity.get("name")
    if not name or not str(name).strip():
        return []

    name_raw = str(name).strip()
    name_norm = norm_name(name_raw)
    name_raw_q = f"%{name_raw}%"
    name_norm_q = f"%{name_norm}%"

    nat = player_identity.get("nationality")
    nat_raw = nat.strip() if isinstance(nat, str) else ""
    nat_q = f"%{nat_raw}%" if nat_raw else None

    rows = db.execute(
        text(
            """
            SELECT id, metadata, content
            FROM player_data
            WHERE
            (
                (metadata->>'player_name_norm') ILIKE :name_norm_q
                OR (metadata->>'player_name') ILIKE :name_raw_q
                OR (content ILIKE :name_raw_q)
            )
            AND (
                :nat_q IS NULL
                OR (metadata->>'nationality_name') ILIKE :nat_q
                OR (content ILIKE :nat_q)
            )
            ORDER BY id DESC
            LIMIT :lim
            """
        ),
        {
            "name_norm_q": name_norm_q,
            "name_raw_q": name_raw_q,
            "nat_q": nat_q,
            "lim": int(limit_docs),
        },
    ).mappings().all()

    if not rows:
        rows = db.execute(
            text(
                """
                SELECT id, metadata, content
                FROM player_data
                WHERE
                (
                    (metadata->>'player_name_norm') ILIKE :name_norm_q
                    OR (metadata->>'player_name') ILIKE :name_raw_q
                    OR (content ILIKE :name_raw_q)
                )
                ORDER BY id DESC
                LIMIT :lim
                """
            ),
            {"name_norm_q": name_norm_q, "name_raw_q": name_raw_q, "lim": int(limit_docs)},
        ).mappings().all()
    if not rows:
        return []

    best: Tuple[float, Optional[int]] = (-1.0, None)
    for row in rows:
        score = _score_candidate(row.get("metadata") or {}, player_identity)
        row_id = row.get("id")
        if row_id is not None and score > best[0]:
            best = (score, int(row_id))

    if best[1] is None:
        return [{"id": row["id"], "content": row.get("content"), "metadata": row.get("metadata")} for row in rows[:limit_docs]]

    doc = db.execute(
        text(
            """
            SELECT id, metadata, content
            FROM player_data
            WHERE id = :id
            LIMIT 1
            """
        ),
        {"id": best[1]},
    ).mappings().first()
    if not doc:
        return []

    return [{"id": doc["id"], "content": doc.get("content"), "metadata": doc.get("metadata")}]


def build_player_card_from_docs(metric_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    card: Dict[str, Any] = {}

    for doc in metric_docs:
        meta = doc.get("metadata") or {}

        fields = {
            "name": _first_non_empty(meta.get("player_name"), meta.get("name"), meta.get("player")),
            "team": _first_non_empty(meta.get("team"), meta.get("team_name"), meta.get("club")),
            "nationality": _first_non_empty(meta.get("nationality"), meta.get("nationality_name"), meta.get("country")),
            "gender": _first_non_empty(meta.get("gender")),
            "age": _first_non_empty(meta.get("age")),
            "height": _first_non_empty(meta.get("height"), meta.get("height_cm")),
            "weight": _first_non_empty(meta.get("weight"), meta.get("weight_kg")),
            "potential": _first_non_empty(meta.get("potential")),
            "form": _first_non_empty(meta.get("form")),
            "position_name": _first_non_empty(meta.get("position_name"), meta.get("position")),
        }
        for key, value in fields.items():
            if key not in card and value is not None:
                card[key] = value

        if "roles" not in card:
            if card.get("position_name"):
                card["roles"] = [str(card["position_name"])]
            else:
                roles_raw = _first_non_empty(meta.get("roles"), meta.get("roles_json"), meta.get("position"), meta.get("position_name"))
                card["roles"] = _normalize_roles(roles_raw)

    if "roles" not in card:
        card["roles"] = [str(card["position_name"])] if card.get("position_name") else []

    return card


def _build_llm_input(player_card: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> str:
    parts: List[str] = ["PLAYER_CARD_JSON:", str(player_card or {}), "\nMETRIC_DOCUMENTS (newest first):"]

    if not metric_docs:
        parts.append("[]")
    else:
        for doc in metric_docs[:30]:
            meta = doc.get("metadata") or {}
            content = (doc.get("content") or "").strip()
            if len(content) > 1200:
                content = content[:1200] + "..."
            parts.append(f"\n- doc_id: {doc.get('id')}")
            parts.append(f"  metadata: {meta}")
            parts.append(f"  content: {content}")

    return "\n".join(parts)


def generate_report_content(
    db,
    favorite_id: str,
    lang: str = "en",
    version: int = 1,
    player_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    identity = player_identity or {}
    docs = fetch_docs_for_favorite(db, player_identity=identity, limit_docs=30)
    player_card = build_player_card_from_docs(docs)

    for key, value in identity.items():
        if key not in player_card and value is not None:
            player_card[key] = value
    for score_key in ("potential", "form"):
        if score_key not in player_card and identity.get(score_key) is not None:
            player_card[score_key] = identity[score_key]

    report_text = (report_chain.invoke({"input_text": _build_llm_input(player_card, docs), "lang": lang}) or "").strip()
    content_json = {
        "favorite_player_id": favorite_id,
        "language": lang,
        "version": version,
        "player_identity": identity,
        "player_card": player_card,
        "metrics_docs": docs,
        "report_text": report_text,
    }
    return {"content": report_text, "content_json": content_json}
