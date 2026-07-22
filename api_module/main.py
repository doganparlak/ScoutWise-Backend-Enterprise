import datetime as dt
import json
import os
import re
import secrets
import unicodedata
import uuid
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import Body, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query as FastAPIQuery, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

load_dotenv()

from api_module.database import SessionLocal, get_db
from api_module.models import (
    LoginIn,
    LoginOut,
    MatchupComparisonIn,
    MatchupComparisonOut,
    EnterpriseFavoritePlayerIn,
    EnterpriseFavoritePlayerOut,
    EnterpriseAllowlistEmailIn,
    EnterpriseLineupIn,
    EnterpriseLineupOut,
    EnterpriseTacticBoardIn,
    EnterpriseTacticBoardOut,
    EnterpriseProChatIn,
    EnterpriseProStrategyIn,
    EnterpriseProStrategyOut,
    EnterpriseScoutingReportIn,
    EnterpriseScoutingReportOut,
    PasswordResetRequestIn,
    PlayerPoolFilterOptionsOut,
    PlayerPoolFormOut,
    PlayerPoolPotentialOut,
    PlayerPoolSearchIn,
    PlayerPoolSearchRow,
    PlayerPoolWeeklyPopularIn,
    SetNewPasswordIn,
    SignupCodeRequestIn,
    SignUpIn,
    VerifyResetIn,
    VerifySignupIn,
)
from api_module.utilities import (
    create_email_code,
    hash_pw,
    normalize_lang,
    revoke_session,
    send_email_code,
    user_row_to_dict,
    verify_email_code,
    verify_pw,
    get_bearer_token,
    now_utc,
    require_auth,
)
from player_pool_module.player_pool import get_player_pool_filter_options, search_players
from player_pool_module.weekly_popular import get_weekly_popular_players, record_player_search
from matchup_module.comparison import get_matchup_comparison
from lineup_module.lineup import create_lineup, delete_lineup, list_lineups, update_lineup
from tactic_board_module.tactic_board import create_tactic_board, delete_tactic_board, list_tactic_boards, update_tactic_board
from potential_form_module.form import reveal_player_form
from potential_form_module.potential import reveal_player_potential
from report_module.report import generate_report_content
from scoutwise_pro_module.pro import (
    get_strategy,
    reset_chat_session,
    save_strategy,
    send_chat,
    user_session_token,
)

PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$")
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

MESSAGES = {
    "weak_pw": {
        "en": "Password must be at least 8 characters and include at least one letter and one number.",
        "tr": "Şifre en az 8 karakter olmalı, en az bir harf ve bir rakam içermeli.",
    },
    "email_registered": {
        "en": "Email already registered",
        "tr": "Bu e-posta zaten kayıtlı",
    },
    "no_pending_signup": {
        "en": "No pending signup for this email",
        "tr": "Bu e-posta için bekleyen kayıt bulunamadı",
    },
    "invalid_or_expired_code": {
        "en": "Invalid or expired code",
        "tr": "Kod geçersiz veya süresi dolmuş",
    },
    "invalid_credentials": {
        "en": "Invalid credentials",
        "tr": "E-posta veya şifre hatalı",
    },
    "verify_email_first": {
        "en": "Please verify your email before logging in",
        "tr": "Giriş yapmadan önce lütfen e-postanı doğrula",
    },
    "verify_reset_first": {
        "en": "Please verify your reset code before setting a new password",
        "tr": "Yeni şifre belirlemeden önce lütfen sıfırlama kodunu doğrula",
    },
    "same_pw": {
        "en": "New password must be different from your current password",
        "tr": "Yeni şifre mevcut şifrenden farklı olmalı",
    },
    "signup_not_allowed": {
        "en": "You are not authorized to sign up. Please contact us via support@scoutwise.ai.",
        "tr": "Kayıt olma yetkiniz tanımlanmamıştır. Lütfen support@scoutwise.ai aracılığı ile iletişime geçin.",
    },
    "admin_only": {
        "en": "Only the ScoutWise admin can manage authorized enterprise users.",
        "tr": "Yetkili enterprise kullanıcılarını yalnızca ScoutWise admin yönetebilir.",
    },
}


def msg(key: str, lang: str | None) -> str:
    preferred = normalize_lang(lang) or "tr"
    return MESSAGES[key][preferred]


ENTERPRISE_ADMIN_EMAILS = {"dgnprlk@gmail.com", "cemzengin@gmail.com"}


def is_enterprise_email_allowed(db: Session, email: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1
            FROM enterprise_auth_allowlist
            WHERE lower(email) = lower(:email)
              AND is_active = TRUE
            LIMIT 1
            """
        ),
        {"email": email.strip()},
    ).first()
    return bool(row)


def require_enterprise_admin(db: Session, user_id: str, accept_language: str | None = None) -> None:
    row = db.execute(
        text("SELECT email FROM enterprise_users WHERE id = :id"),
        {"id": user_id},
    ).mappings().first()
    if not row or str(row["email"]).strip().lower() not in ENTERPRISE_ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail=msg("admin_only", accept_language))

app = FastAPI(title="ScoutWise Enterprise Backend")

origins_env = os.environ.get("CORS_ORIGINS")
origins = [origin.strip() for origin in origins_env.split(",")] if origins_env else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_pro_chat_route(request: Request, call_next):
    if request.url.path == "/pro/chat":
        print("[enterprise_pro_chat] event=route_hit path=/pro/chat", flush=True)
    response = await call_next(request)
    if request.url.path == "/pro/chat":
        print(f"[enterprise_pro_chat] event=route_done status={response.status_code}", flush=True)
    return response


@app.on_event("startup")
def ensure_enterprise_sessions_table() -> None:
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.enterprise_auth_allowlist (
                  id BIGSERIAL PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  is_active BOOLEAN NOT NULL DEFAULT TRUE,
                  note TEXT,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS enterprise_auth_allowlist_email_idx
                ON public.enterprise_auth_allowlist (lower(email))
                """
            )
        )
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.enterprise_sessions (
                  token TEXT PRIMARY KEY,
                  user_id UUID NOT NULL REFERENCES public.enterprise_users(id) ON DELETE CASCADE,
                  language TEXT DEFAULT 'tr' CHECK (language IN ('en', 'tr')),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  expires_at TIMESTAMPTZ NOT NULL,
                  revoked_at TIMESTAMPTZ
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_enterprise_sessions_user_id
                ON public.enterprise_sessions (user_id)
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_enterprise_sessions_expires_at
                ON public.enterprise_sessions (expires_at)
                """
            )
        )
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.enterprise_player_pool_scouting_reports (
                  id UUID PRIMARY KEY,
                  user_id UUID NOT NULL REFERENCES public.enterprise_users(id) ON DELETE CASCADE,
                  cache_key TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'processing',
                  language TEXT DEFAULT 'en',
                  version INTEGER NOT NULL DEFAULT 1,
                  player_name TEXT,
                  player_payload JSONB,
                  content TEXT,
                  content_json JSONB,
                  error TEXT,
                  ready_at TIMESTAMPTZ,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  UNIQUE (user_id, cache_key, language, version)
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_enterprise_player_pool_scouting_reports_user
                ON public.enterprise_player_pool_scouting_reports (user_id, created_at DESC)
                """
            )
        )
        db.commit()
    finally:
        db.close()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/pro/strategy", response_model=EnterpriseProStrategyOut)
def pro_strategy_get(
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return get_strategy(db, user_id)


@app.put("/pro/strategy", response_model=EnterpriseProStrategyOut)
def pro_strategy_put(
    payload: EnterpriseProStrategyIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return save_strategy(db, user_id, payload)


@app.post("/pro/chat")
def pro_chat(
    payload: EnterpriseProChatIn,
    user_id: str = Depends(require_auth),
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    return send_chat(db, user_id=user_id, payload=payload, accept_language=accept_language)


@app.post("/pro/chat/reset")
def pro_chat_reset(
    session_id: str = FastAPIQuery("default"),
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    token = user_session_token(user_id, session_id)
    return reset_chat_session(db, token=token)


@app.post("/admin/enterprise-auth-allowlist")
def add_enterprise_auth_allowlist_email(
    payload: EnterpriseAllowlistEmailIn,
    user_id: str = Depends(require_auth),
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    require_enterprise_admin(db, user_id, accept_language)
    email = payload.email.strip().lower()
    note = payload.note or "Added from enterprise portal admin"
    row = db.execute(
        text(
            """
            INSERT INTO enterprise_auth_allowlist (email, is_active, note)
            VALUES (:email, TRUE, :note)
            ON CONFLICT (email) DO UPDATE
            SET is_active = TRUE,
                note = EXCLUDED.note,
                updated_at = NOW()
            RETURNING id, email, is_active, note, created_at, updated_at
            """
        ),
        {"email": email, "note": note},
    ).mappings().first()
    db.commit()
    return {"ok": True, "email": row["email"], "isActive": row["is_active"]}


@app.post("/auth/signup")
def signup(
    payload: SignUpIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    email_norm = payload.email.strip()
    preferred = normalize_lang(payload.uiLanguage) or normalize_lang(accept_language) or "tr"

    if not is_enterprise_email_allowed(db, email_norm):
        raise HTTPException(status_code=403, detail=msg("signup_not_allowed", preferred))

    if not PASSWORD_RE.match(payload.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=msg("weak_pw", preferred),
        )

    existing = db.execute(
        text(
            """
            SELECT id, is_email_verified
            FROM enterprise_users
            WHERE lower(email) = lower(:email)
            """
        ),
        {"email": email_norm},
    ).mappings().first()

    if existing and existing["is_email_verified"]:
        raise HTTPException(status_code=400, detail=msg("email_registered", preferred))

    password_hash = hash_pw(payload.password)
    user_id = None

    if existing:
        db.execute(
            text(
                """
                UPDATE enterprise_users
                SET password_hash = :password_hash,
                    ui_language = :ui_language,
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {
                "id": existing["id"],
                "password_hash": password_hash,
                "ui_language": preferred,
            },
        )
        user_id = str(existing["id"])
    else:
        inserted = db.execute(
            text(
                """
                INSERT INTO enterprise_users (email, password_hash, ui_language, is_email_verified)
                VALUES (:email, :password_hash, :ui_language, FALSE)
                RETURNING id
                """
            ),
            {
                "email": email_norm,
                "password_hash": password_hash,
                "ui_language": preferred,
            },
        ).mappings().first()
        user_id = str(inserted["id"])

    db.commit()

    code = create_email_code(db, user_id=user_id, email=email_norm, purpose="signup")
    send_email_code(email_norm, code, mail_type="signup", lang=preferred)
    return {"ok": True, "verificationRequired": True, "codeSent": True}


@app.post("/auth/request_signup_code")
def request_signup_code(
    body: SignupCodeRequestIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    email = body.email.strip()
    preferred = normalize_lang(accept_language) or "tr"
    if not is_enterprise_email_allowed(db, email):
        raise HTTPException(status_code=403, detail=msg("signup_not_allowed", preferred))

    row = db.execute(
        text(
            """
            SELECT id, ui_language, is_email_verified
            FROM enterprise_users
            WHERE lower(email) = lower(:email)
            """
        ),
        {"email": email},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=400, detail=msg("no_pending_signup", preferred))
    if row["is_email_verified"]:
        return {"ok": True}

    preferred = normalize_lang(accept_language) or normalize_lang(row["ui_language"]) or "tr"
    code = create_email_code(db, user_id=str(row["id"]), email=email, purpose="signup")
    send_email_code(email, code, mail_type="signup", lang=preferred)
    return {"ok": True}


@app.post("/auth/verify_signup_code")
def verify_signup_code(
    body: VerifySignupIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    email = body.email.strip()
    user_id = verify_email_code(db, email=email, code=body.code.strip(), purpose="signup")
    if not user_id:
        raise HTTPException(status_code=400, detail=msg("invalid_or_expired_code", accept_language))

    db.execute(
        text(
            """
            UPDATE enterprise_users
            SET is_email_verified = TRUE,
                updated_at = NOW()
            WHERE id = :id
            """
        ),
        {"id": user_id},
    )
    db.commit()

    row = db.execute(
        text("SELECT * FROM enterprise_users WHERE id = :id"),
        {"id": user_id},
    ).mappings().first()

    token = secrets.token_urlsafe(32)
    expires_at = now_utc() + dt.timedelta(days=SESSION_TTL_DAYS)
    db.execute(
        text(
            """
            INSERT INTO enterprise_sessions (token, user_id, language, created_at, expires_at)
            VALUES (:token, :user_id, :language, NOW(), :expires_at)
            """
        ),
        {
            "token": token,
            "user_id": row["id"],
            "language": row["ui_language"] or "tr",
            "expires_at": expires_at,
        },
    )
    db.commit()
    return {"token": token, "user": user_row_to_dict(row)}


@app.post("/auth/request_reset")
def request_reset(
    body: PasswordResetRequestIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    email = body.email.strip()
    preferred = normalize_lang(accept_language) or "tr"
    if not is_enterprise_email_allowed(db, email):
        raise HTTPException(status_code=403, detail=msg("signup_not_allowed", preferred))

    row = db.execute(
        text(
            """
            SELECT id, ui_language
            FROM enterprise_users
            WHERE lower(email) = lower(:email)
              AND is_email_verified = TRUE
            """
        ),
        {"email": email},
    ).mappings().first()

    if row:
        preferred = normalize_lang(accept_language) or normalize_lang(row["ui_language"]) or "tr"
        code = create_email_code(db, user_id=str(row["id"]), email=email, purpose="reset")
        send_email_code(email, code, mail_type="reset", lang=preferred)

    return {"ok": True}


@app.post("/auth/verify_reset")
def verify_reset(
    body: VerifyResetIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    user_id = verify_email_code(db, email=body.email.strip(), code=body.code.strip(), purpose="reset")
    if not user_id:
        raise HTTPException(status_code=400, detail=msg("invalid_or_expired_code", accept_language))
    return {"ok": True}


@app.post("/auth/set_new_password")
def set_new_password(
    body: SetNewPasswordIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not PASSWORD_RE.match(body.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=msg("weak_pw", accept_language),
        )

    email = body.email.strip()
    row = db.execute(
        text(
            """
            SELECT id, password_hash
            FROM enterprise_users
            WHERE lower(email) = lower(:email)
              AND is_email_verified = TRUE
            """
        ),
        {"email": email},
    ).mappings().first()

    if not row:
        return {"ok": True}

    reset_was_verified = db.execute(
        text(
            """
            SELECT 1
            FROM enterprise_password_reset_codes
            WHERE lower(email) = lower(:email)
              AND user_id = :user_id
              AND consumed_at IS NOT NULL
              AND consumed_at > NOW() - INTERVAL '10 minutes'
            ORDER BY consumed_at DESC
            LIMIT 1
            """
        ),
        {"email": email, "user_id": row["id"]},
    ).first()

    if not reset_was_verified:
        raise HTTPException(status_code=403, detail=msg("verify_reset_first", accept_language))

    new_hash = hash_pw(body.new_password)
    if verify_pw(body.new_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail=msg("same_pw", accept_language))

    db.execute(
        text(
            """
            UPDATE enterprise_users
            SET password_hash = :password_hash,
                updated_at = NOW()
            WHERE id = :id
            """
        ),
        {"password_hash": new_hash, "id": row["id"]},
    )
    db.execute(text("DELETE FROM enterprise_sessions WHERE user_id = :id"), {"id": row["id"]})
    db.execute(
        text(
            """
            UPDATE enterprise_password_reset_codes
            SET consumed_at = NOW()
            WHERE lower(email) = lower(:email)
              AND user_id = :user_id
            """
        ),
        {"email": email, "user_id": row["id"]},
    )
    db.commit()
    return {"ok": True}


@app.post("/auth/login", response_model=LoginOut)
def login(
    payload: LoginIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text(
            """
            SELECT *
            FROM enterprise_users
            WHERE lower(email) = lower(:email)
            """
        ),
        {"email": payload.email.strip()},
    ).mappings().first()

    preferred = normalize_lang(payload.uiLanguage) or normalize_lang(accept_language) or "tr"

    if not row or not verify_pw(payload.password, row["password_hash"]):
        raise HTTPException(status_code=400, detail=msg("invalid_credentials", preferred))

    if not row["is_email_verified"]:
        raise HTTPException(status_code=403, detail=msg("verify_email_first", preferred))

    if preferred:
        db.execute(
            text("UPDATE enterprise_users SET ui_language = :lang WHERE id = :id"),
            {"lang": preferred, "id": row["id"]},
        )
        db.commit()

    row = db.execute(
        text("SELECT * FROM enterprise_users WHERE id = :id"),
        {"id": row["id"]},
    ).mappings().first()

    token = secrets.token_urlsafe(32)
    expires_at = now_utc() + dt.timedelta(days=SESSION_TTL_DAYS)
    db.execute(
        text(
            """
            INSERT INTO enterprise_sessions (token, user_id, language, created_at, expires_at)
            VALUES (:token, :user_id, :language, NOW(), :expires_at)
            """
        ),
        {
            "token": token,
            "user_id": row["id"],
            "language": row["ui_language"] or "tr",
            "expires_at": expires_at,
        },
    )
    db.commit()

    return {"token": token, "user": user_row_to_dict(row)}


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(authorization: str | None = Header(None), db: Session = Depends(get_db)):
    try:
        token = get_bearer_token(authorization)
    except HTTPException:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    revoke_session(db, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _metadata_text(metadata: Dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        text_value = str(value).strip()
        if text_value:
            return text_value
    return None


def _metadata_int(metadata: Dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = metadata.get(key)
        if value is None or value == "":
            continue
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            continue
    return None


def _metadata_roles(metadata: Dict[str, Any]) -> list[str]:
    roles: list[str] = []

    def add_role(value: Any) -> None:
        role = str(value or "").strip()
        if role and role not in roles:
            roles.append(role)

    for key in ("roles", "positions", "position_names_seen"):
        raw_roles = metadata.get(key)
        if isinstance(raw_roles, list):
            for role in raw_roles:
                add_role(role)

    position_counts = metadata.get("position_counts")
    if isinstance(position_counts, dict):
        for role in position_counts.keys():
            add_role(role)

    add_role(metadata.get("primary_position_code"))
    add_role(_metadata_text(metadata, "position_name", "position", "role"))
    return roles


def _favorite_out(row: Any) -> EnterpriseFavoritePlayerOut:
    data = row._mapping if hasattr(row, "_mapping") else row
    roles_raw = data.get("roles_json") or []
    if isinstance(roles_raw, str):
        try:
            roles_raw = json.loads(roles_raw)
        except json.JSONDecodeError:
            roles_raw = []

    position_counts_raw = data.get("position_counts") or {}
    if isinstance(position_counts_raw, str):
        try:
            position_counts_raw = json.loads(position_counts_raw)
        except json.JSONDecodeError:
            position_counts_raw = {}
    position_counts = {
        str(role): int(count)
        for role, count in (position_counts_raw.items() if isinstance(position_counts_raw, dict) else [])
        if str(role).strip() and isinstance(count, (int, float)) and int(count) > 0
    }
    position_counts = dict(sorted(position_counts.items(), key=lambda item: (-item[1], item[0])))

    position_names_raw = data.get("position_names_seen") or []
    if isinstance(position_names_raw, str):
        try:
            position_names_raw = json.loads(position_names_raw)
        except json.JSONDecodeError:
            position_names_raw = []
    position_names_seen = [str(role).strip() for role in position_names_raw if str(role).strip()] if isinstance(position_names_raw, list) else []
    if not position_names_seen:
        position_names_seen = list(position_counts.keys())

    position_count_total = data.get("position_count_total")
    if position_count_total is None:
        position_count_total = sum(position_counts.values())

    return EnterpriseFavoritePlayerOut(
        id=str(data["id"]),
        clubPlayerId=data.get("club_player_id"),
        name=data["name"],
        nationality=data.get("nationality"),
        age=data.get("age"),
        potential=data.get("potential"),
        form=data.get("form"),
        gender=data.get("gender"),
        height=data.get("height"),
        weight=data.get("weight"),
        team=data.get("team"),
        league=data.get("league"),
        roles=[str(role) for role in roles_raw if str(role).strip()],
        positionCounts=position_counts,
        positionCountTotal=int(position_count_total or 0),
        positionNamesSeen=position_names_seen,
        primaryPositionCode=data.get("primary_position_code") or (position_names_seen[0] if position_names_seen else None),
    )


def _enterprise_favorite_identity(row: Any) -> Dict[str, Any]:
    data = row._mapping if hasattr(row, "_mapping") else row
    roles_raw = data.get("roles_json") or []
    if isinstance(roles_raw, str):
        try:
            roles_raw = json.loads(roles_raw)
        except json.JSONDecodeError:
            roles_raw = []
    position_counts_raw = data.get("position_counts") or {}
    if isinstance(position_counts_raw, str):
        try:
            position_counts_raw = json.loads(position_counts_raw)
        except json.JSONDecodeError:
            position_counts_raw = {}
    position_counts = position_counts_raw if isinstance(position_counts_raw, dict) else {}

    position_names_raw = data.get("position_names_seen") or []
    if isinstance(position_names_raw, str):
        try:
            position_names_raw = json.loads(position_names_raw)
        except json.JSONDecodeError:
            position_names_raw = []
    position_names_seen = position_names_raw if isinstance(position_names_raw, list) else []

    return {
        "favorite_id": str(data["id"]),
        "club_player_id": data.get("club_player_id"),
        "name": data.get("name"),
        "nationality": data.get("nationality"),
        "age": data.get("age"),
        "potential": data.get("potential"),
        "form": data.get("form"),
        "gender": data.get("gender"),
        "height": data.get("height"),
        "weight": data.get("weight"),
        "team": data.get("team"),
        "league": data.get("league"),
        "roles": [str(role) for role in roles_raw if str(role).strip()] if isinstance(roles_raw, list) else [],
        "position_counts": position_counts,
        "position_count_total": int(data.get("position_count_total") or sum(int(v) for v in position_counts.values() if isinstance(v, (int, float)))),
        "position_names_seen": [str(role) for role in position_names_seen if str(role).strip()],
        "primary_position_code": data.get("primary_position_code"),
    }


def _get_owned_enterprise_favorite(db: Session, favorite_id: str, user_id: str) -> Any:
    row = db.execute(
        text(
            """
            SELECT efp.id, efp.club_player_id, efp.name, efp.nationality, efp.age, efp.potential, efp.form,
                   efp.gender, efp.height, efp.weight, efp.team, efp.league, efp.roles_json,
                   pd.metadata->'position_counts' AS position_counts,
                   pd.metadata->>'position_count_total' AS position_count_total,
                   pd.metadata->'position_names_seen' AS position_names_seen,
                   pd.metadata->>'primary_position_code' AS primary_position_code
            FROM enterprise_favorite_players efp
            LEFT JOIN player_data pd ON pd.id = efp.club_player_id
            WHERE efp.id = :favorite_id
              AND efp.user_id = :user_id
            LIMIT 1
            """
        ),
        {"favorite_id": favorite_id, "user_id": user_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return row


def _generate_enterprise_report_background(
    report_id: str,
    favorite_id: str,
    user_id: str,
    lang: str,
    version: int,
    player_payload: Dict[str, Any],
) -> None:
    db = SessionLocal()
    try:
        generated = generate_report_content(
            db,
            favorite_id=favorite_id,
            lang=lang,
            version=version,
            player_identity=player_payload,
        )
        db.execute(
            text(
                """
                UPDATE enterprise_scouting_reports
                SET status = 'ready',
                    content = :content,
                    content_json = CAST(:content_json AS jsonb),
                    error = NULL,
                    ready_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
                  AND user_id = :user_id
                  AND favorite_player_id = :favorite_id
                """
            ),
            {
                "id": report_id,
                "user_id": user_id,
                "favorite_id": favorite_id,
                "content": generated["content"],
                "content_json": json.dumps(generated["content_json"], ensure_ascii=False, default=str),
            },
        )
        db.commit()
    except Exception as exc:
        print(f"[enterprise_report_generation_failed] report_id={report_id} favorite_id={favorite_id} error={exc}")
        db.execute(
            text(
                """
                UPDATE enterprise_scouting_reports
                SET status = 'failed',
                    error = :error,
                    updated_at = NOW()
                WHERE id = :id
                  AND user_id = :user_id
                  AND favorite_player_id = :favorite_id
                """
            ),
            {"id": report_id, "user_id": user_id, "favorite_id": favorite_id, "error": str(exc)},
        )
        db.commit()
    finally:
        db.close()


def _get_player_metadata(db: Session, player_id: str, world_cup_mode: bool) -> Dict[str, Any]:
    table_name = "player_data_wc" if world_cup_mode else "player_data"
    row = db.execute(
        text(f"""
        SELECT metadata
        FROM {table_name}
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id},
    ).mappings().first()
    if not row or not row["metadata"]:
        raise HTTPException(status_code=404, detail="Player not found")
    return dict(row["metadata"])


def _resolve_club_player_row(db: Session, player_id: str, world_cup_mode: bool) -> Any:
    if not world_cup_mode:
        row = db.execute(
            text("""
            SELECT id, metadata
            FROM player_data
            WHERE id = :player_id
            LIMIT 1
            """),
            {"player_id": player_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Club player not found")
        return row

    wc_metadata = _get_player_metadata(db, player_id, True)
    player_name = _metadata_text(wc_metadata, "player_name", "name")
    gender = _metadata_text(wc_metadata, "gender")
    age = _metadata_int(wc_metadata, "age")
    height = _metadata_text(wc_metadata, "height")
    weight = _metadata_text(wc_metadata, "weight")

    row = db.execute(
        text("""
        SELECT id, metadata
        FROM player_data
        WHERE LOWER(TRIM(metadata->>'player_name')) = LOWER(TRIM(:player_name))
          AND (:gender IS NULL OR LOWER(COALESCE(metadata->>'gender', '')) = LOWER(:gender))
          AND (:age IS NULL OR (
                COALESCE(metadata->>'age', '') ~ '^-?[0-9]+([.][0-9]+)?$'
                AND (metadata->>'age')::numeric = :age
              ))
        ORDER BY
          CASE WHEN :height IS NOT NULL AND COALESCE(metadata->>'height', '') = :height THEN 0 ELSE 1 END,
          CASE WHEN :weight IS NOT NULL AND COALESCE(metadata->>'weight', '') = :weight THEN 0 ELSE 1 END,
          id DESC
        LIMIT 1
        """),
        {
            "player_name": player_name,
            "gender": gender,
            "age": age,
            "height": height,
            "weight": weight,
        },
    ).mappings().first()
    if row:
        return row

    nationality = _metadata_text(wc_metadata, "nationality_name", "nationality")
    roles = _metadata_roles(wc_metadata)
    name_tokens = [token for token in re.split(r"\s+", player_name or "") if token]
    last_name = re.sub(r"[^\wÀ-ž'-]", "", name_tokens[-1]) if name_tokens else ""
    last_name = last_name if last_name and last_name.lower() != player_name.lower() else None
    attempts = [
        ("name_gender_age", {"name": player_name, "gender": gender, "minAge": age, "maxAge": age}),
        ("name_nationality_gender", {"name": player_name, "nationality": nationality, "gender": gender}),
        ("name_nationality", {"name": player_name, "nationality": nationality}),
        ("last_name_gender_age", {"name": last_name, "gender": gender, "minAge": age, "maxAge": age}),
        ("last_name_nationality", {"name": last_name, "nationality": nationality}),
        ("name_role", {"name": player_name, "position": roles[0] if roles else None}),
        ("last_name_role", {"name": last_name, "position": roles[0] if roles else None}),
        ("name_only", {"name": player_name}),
        ("last_name_only", {"name": last_name}),
    ]

    for stage, filters in attempts:
        search_filters = {key: value for key, value in filters.items() if value not in (None, "", [])}
        search_filters["limit"] = 10
        search_filters["worldCupMode"] = False
        rows = search_players(db, search_filters)
        print(
            "[enterprise_club_resolve] "
            f"event=fallback stage={stage} wc_player_id={player_id!r} name={player_name!r} "
            f"nationality={nationality!r} gender={gender!r} matches={len(rows)}",
            flush=True,
        )
        if not rows:
            continue

        source_payload = {
            "name": player_name,
            "nationality": nationality,
            "gender": gender,
            "age": age,
        }
        for best in rows:
            club_row = db.execute(
                text("""
                SELECT id, metadata
                FROM player_data
                WHERE id = :player_id
                LIMIT 1
                """),
                {"player_id": best["id"]},
            ).mappings().first()
            if not club_row:
                continue
            if not _is_safe_enterprise_club_candidate(dict(club_row["metadata"] or {}), source_payload, stage):
                print(
                    "[enterprise_club_resolve] "
                    f"event=rejected_candidate stage={stage} wc_player_id={player_id!r} "
                    f"candidate_id={club_row['id']!r} name={player_name!r}",
                    flush=True,
                )
                continue
            print(
                "[enterprise_club_resolve] "
                f"event=resolved stage={stage} wc_player_id={player_id!r} club_player_id={club_row['id']!r} "
                f"name={player_name!r}",
                flush=True,
            )
            return club_row

    raise HTTPException(status_code=404, detail="Matching club player not found")


def _fold_identity_text(value: Any) -> str:
    text_value = str(value or "").strip().lower()
    text_value = unicodedata.normalize("NFKD", text_value)
    text_value = "".join(char for char in text_value if not unicodedata.combining(char))
    text_value = re.sub(r"[^a-z0-9]+", " ", text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def _token_set(value: Any) -> set[str]:
    return {token for token in _fold_identity_text(value).split() if token}


def _is_safe_enterprise_club_candidate(
    candidate_metadata: Dict[str, Any],
    source_payload: Dict[str, Any],
    stage: str,
) -> bool:
    source_name = source_payload.get("name")
    candidate_name = _metadata_text(candidate_metadata, "player_name", "name")
    source_tokens = _token_set(source_name)
    candidate_tokens = _token_set(candidate_name)
    if not source_tokens or not candidate_tokens:
        return False

    source_name_folded = _fold_identity_text(source_name)
    candidate_name_folded = _fold_identity_text(candidate_name)
    full_name_match = source_name_folded == candidate_name_folded
    token_covered = source_tokens.issubset(candidate_tokens) or candidate_tokens.issubset(source_tokens)
    if stage.startswith("last_name") and not full_name_match:
        source_last = next(reversed(source_name_folded.split()), "")
        candidate_last = next(reversed(candidate_name_folded.split()), "")
        if not source_last or source_last != candidate_last:
            return False
        if len(source_tokens) > 1 and len(source_tokens.intersection(candidate_tokens)) < 2:
            return False
    elif not (full_name_match or token_covered):
        return False

    source_team = _fold_identity_text(source_payload.get("team"))
    candidate_team = _fold_identity_text(_metadata_text(candidate_metadata, "team_name", "team", "club"))
    if source_team and candidate_team and source_team != candidate_team:
        return False

    source_nationality = _fold_identity_text(source_payload.get("nationality"))
    candidate_nationality = _fold_identity_text(_metadata_text(candidate_metadata, "nationality_name", "nationality"))
    nationality_aliases = {
        "turkey": {"turkey", "turkiye", "turkiye"},
        "turkiye": {"turkey", "turkiye", "turkiye"},
    }
    if source_nationality and candidate_nationality:
        accepted = nationality_aliases.get(source_nationality, {source_nationality})
        if candidate_nationality not in accepted:
            return False

    source_gender = _fold_identity_text(source_payload.get("gender"))
    candidate_gender = _fold_identity_text(_metadata_text(candidate_metadata, "gender"))
    if source_gender and candidate_gender and source_gender != candidate_gender:
        return False

    source_age = source_payload.get("age")
    candidate_age = _metadata_int(candidate_metadata, "age")
    if source_age is not None and candidate_age is not None:
        try:
            if abs(int(source_age) - int(candidate_age)) > 1:
                return False
        except (TypeError, ValueError):
            pass

    return True


def _resolve_club_player_row_from_favorite_payload(
    db: Session,
    payload: EnterpriseFavoritePlayerIn,
) -> Any | None:
    roles = [role for role in (payload.roles or []) if role]
    attempts = [
        ("name_team_nationality_gender", {"name": payload.name, "team": payload.team, "nationality": payload.nationality, "gender": payload.gender}),
        ("name_team_nationality", {"name": payload.name, "team": payload.team, "nationality": payload.nationality}),
        ("name_team", {"name": payload.name, "team": payload.team}),
        ("name_nationality", {"name": payload.name, "nationality": payload.nationality}),
        ("name_only", {"name": payload.name}),
    ]

    rows = []
    winning_stage = "none"
    for stage, filters in attempts:
        search_filters = {key: value for key, value in filters.items() if value not in (None, "", [])}
        search_filters["limit"] = 10
        search_filters["worldCupMode"] = False
        rows = search_players(db, search_filters)
        print(
            "[enterprise_favorite_save] "
            f"event=snapshot_resolve stage={stage} name={payload.name!r} team={payload.team!r} "
            f"nationality={payload.nationality!r} role={(roles[0] if roles else None)!r} "
            f"matches={len(rows)}",
            flush=True,
        )
        if rows:
            winning_stage = stage
            break

    if not rows:
        return None

    source_payload = {
        "name": payload.name,
        "team": payload.team,
        "nationality": payload.nationality,
        "gender": payload.gender,
        "age": payload.age,
    }
    best = next(
        (
            row
            for row in rows
            if _is_safe_enterprise_club_candidate(row.get("content") or {}, source_payload, winning_stage)
        ),
        None,
    )
    if not best:
        print(
            "[enterprise_favorite_save] "
            f"event=snapshot_resolve_rejected_all stage={winning_stage} name={payload.name!r} "
            f"team={payload.team!r} nationality={payload.nationality!r}",
            flush=True,
        )
        return None

    player_id = str(best["id"])
    print(
        "[enterprise_favorite_save] "
        f"event=snapshot_resolved stage={winning_stage} player_id={player_id} player={payload.name!r}",
        flush=True,
    )
    return _resolve_club_player_row(db, player_id, False)


def _favorite_values_from_club_row(
    club_row: Any,
    potential: int,
    form: int,
) -> Dict[str, Any]:
    metadata = dict(club_row["metadata"] or {})
    return {
        "club_player_id": int(club_row["id"]),
        "name": _metadata_text(metadata, "player_name", "name") or "",
        "nationality": _metadata_text(metadata, "nationality_name", "nationality"),
        "age": _metadata_int(metadata, "age"),
        "potential": potential,
        "form": form,
        "gender": _metadata_text(metadata, "gender"),
        "height": _metadata_text(metadata, "height"),
        "weight": _metadata_text(metadata, "weight"),
        "team": _metadata_text(metadata, "team_name", "team"),
        "league": _metadata_text(metadata, "league_name", "league"),
        "roles": _metadata_roles(metadata),
    }


def _favorite_values_from_payload(payload: EnterpriseFavoritePlayerIn) -> Dict[str, Any]:
    return {
        "club_player_id": None,
        "name": (payload.name or "").strip(),
        "nationality": (payload.nationality or "").strip() or None,
        "age": payload.age,
        "potential": payload.potential if payload.potential is not None else 0,
        "form": payload.form if payload.form is not None else 0,
        "gender": (payload.gender or "").strip() or None,
        "height": str(payload.height).strip() if payload.height is not None else None,
        "weight": str(payload.weight).strip() if payload.weight is not None else None,
        "team": (payload.team or "").strip() or None,
        "league": (payload.league or "").strip() or None,
        "roles": [str(role).strip() for role in (payload.roles or []) if str(role).strip()],
    }


@app.post("/player-pool/search", response_model=list[PlayerPoolSearchRow])
def player_pool_search(
    payload: PlayerPoolSearchIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    if payload.minAge is not None and payload.maxAge is not None and payload.minAge > payload.maxAge:
        raise HTTPException(status_code=400, detail="minAge cannot be greater than maxAge")
    if payload.minHeight is not None and payload.maxHeight is not None and payload.minHeight > payload.maxHeight:
        raise HTTPException(status_code=400, detail="minHeight cannot be greater than maxHeight")
    if payload.minWeight is not None and payload.maxWeight is not None and payload.minWeight > payload.maxWeight:
        raise HTTPException(status_code=400, detail="minWeight cannot be greater than maxWeight")

    return search_players(db, payload.model_dump(exclude_none=True))


@app.post("/player-pool/{player_id}/search-hit")
def player_pool_record_search_hit(
    player_id: str,
    worldCupMode: bool = FastAPIQuery(False),
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    try:
        record_player_search(db, player_id, worldCupMode)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid player_id") from None

    db.commit()
    return {"ok": True}


@app.post("/player-pool/{player_id}/potential", response_model=PlayerPoolPotentialOut)
def player_pool_reveal_potential(
    player_id: str,
    worldCupMode: bool = FastAPIQuery(False),
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    if worldCupMode:
        raise HTTPException(status_code=400, detail="Potential is not available in World Cup mode")
    try:
        return reveal_player_potential(db, player_id, worldCupMode)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Potential reveal failed") from exc


@app.post("/player-pool/{player_id}/form", response_model=PlayerPoolFormOut)
def player_pool_reveal_form(
    player_id: str,
    worldCupMode: bool = FastAPIQuery(False),
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    try:
        return reveal_player_form(db, player_id, worldCupMode)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Form reveal failed") from exc


@app.post("/player-pool/weekly-popular", response_model=list[PlayerPoolSearchRow])
def player_pool_weekly_popular(
    payload: PlayerPoolWeeklyPopularIn = Body(default=PlayerPoolWeeklyPopularIn()),
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    return get_weekly_popular_players(db, payload.limit or 10, bool(payload.worldCupMode))


@app.post("/player-pool/matchup/comparison", response_model=MatchupComparisonOut)
def player_pool_matchup_comparison(
    payload: MatchupComparisonIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    try:
        return get_matchup_comparison(db, payload.player1Id, payload.player2Id, bool(payload.worldCupMode))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/player-pool/options", response_model=PlayerPoolFilterOptionsOut)
def player_pool_options(
    worldCupMode: bool = FastAPIQuery(False),
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    return get_player_pool_filter_options(db, worldCupMode)


@app.get("/lineups", response_model=list[EnterpriseLineupOut])
def list_enterprise_lineups(
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return list_lineups(db, user_id)


@app.post("/lineups", response_model=EnterpriseLineupOut)
def create_enterprise_lineup(
    payload: EnterpriseLineupIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return create_lineup(db, user_id, payload)


@app.patch("/lineups/{lineup_id}", response_model=EnterpriseLineupOut)
def update_enterprise_lineup(
    lineup_id: str,
    payload: EnterpriseLineupIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return update_lineup(db, user_id, lineup_id, payload)


@app.delete("/lineups/{lineup_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_enterprise_lineup(
    lineup_id: str,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return delete_lineup(db, user_id, lineup_id)


@app.get("/tactic-boards", response_model=list[EnterpriseTacticBoardOut])
def list_enterprise_tactic_boards(
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return list_tactic_boards(db, user_id)


@app.post("/tactic-boards", response_model=EnterpriseTacticBoardOut)
def create_enterprise_tactic_board(
    payload: EnterpriseTacticBoardIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return create_tactic_board(db, user_id, payload)


@app.patch("/tactic-boards/{board_id}", response_model=EnterpriseTacticBoardOut)
def update_enterprise_tactic_board(
    board_id: str,
    payload: EnterpriseTacticBoardIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return update_tactic_board(db, user_id, board_id, payload)


@app.delete("/tactic-boards/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_enterprise_tactic_board(
    board_id: str,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return delete_tactic_board(db, user_id, board_id)


@app.get("/favorite-players", response_model=list[EnterpriseFavoritePlayerOut])
def list_enterprise_favorite_players(
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
        SELECT efp.id, efp.club_player_id, efp.name, efp.nationality, efp.age, efp.potential, efp.form,
               efp.gender, efp.height, efp.weight, efp.team, efp.league, efp.roles_json, efp.created_at,
               pd.metadata->'position_counts' AS position_counts,
               pd.metadata->>'position_count_total' AS position_count_total,
               pd.metadata->'position_names_seen' AS position_names_seen,
               pd.metadata->>'primary_position_code' AS primary_position_code
        FROM enterprise_favorite_players efp
        LEFT JOIN player_data pd ON pd.id = efp.club_player_id
        WHERE efp.user_id = :user_id
        ORDER BY efp.created_at DESC
        """),
        {"user_id": user_id},
    ).mappings().all()
    return [_favorite_out(row) for row in rows]


@app.post("/favorite-players", response_model=EnterpriseFavoritePlayerOut)
def save_enterprise_favorite_player(
    payload: EnterpriseFavoritePlayerIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    print(
        "[enterprise_favorite_save] "
        f"event=request has_player_id={bool(payload.playerId)} "
        f"player_id={payload.playerId!r} name={payload.name!r} team={payload.team!r}",
        flush=True,
    )
    if payload.playerId:
        club_row = _resolve_club_player_row(db, payload.playerId, bool(payload.worldCupMode))
    else:
        club_row = _resolve_club_player_row_from_favorite_payload(db, payload)
        if club_row is None:
            print(
                "[enterprise_favorite_save] "
                f"event=snapshot_resolve_failed name={payload.name!r} team={payload.team!r}",
                flush=True,
            )
            favorite_values = _favorite_values_from_payload(payload)
        else:
            payload.playerId = str(club_row["id"])

    if payload.playerId:
        club_player_id = int(club_row["id"])
        potential = reveal_player_potential(db, club_player_id, False)["potential"]
        form = reveal_player_form(db, club_player_id, False)["form"]
        favorite_values = _favorite_values_from_club_row(club_row, potential, form)

    if not favorite_values["name"]:
        raise HTTPException(status_code=400, detail="Player name is missing")

    existing = db.execute(
        text("""
        SELECT id
        FROM enterprise_favorite_players
        WHERE user_id = :user_id
          AND lower(name) = lower(:name)
          AND lower(COALESCE(nationality, '')) = lower(COALESCE(:nationality, ''))
        LIMIT 1
        """),
        {
            "user_id": user_id,
            "name": favorite_values["name"],
            "nationality": favorite_values["nationality"],
        },
    ).mappings().first()

    favorite_id = str(existing["id"]) if existing else str(uuid.uuid4())
    params = {
        "id": favorite_id,
        "user_id": user_id,
        "player_id": str(favorite_values["club_player_id"]) if favorite_values["club_player_id"] is not None else None,
        "player_name": favorite_values["name"],
        "club_player_id": favorite_values["club_player_id"],
        "name": favorite_values["name"],
        "nationality": favorite_values["nationality"],
        "age": favorite_values["age"],
        "potential": favorite_values["potential"],
        "form": favorite_values["form"],
        "gender": favorite_values["gender"],
        "height": favorite_values["height"],
        "weight": favorite_values["weight"],
        "team": favorite_values["team"],
        "league": favorite_values["league"],
        "roles_json": json.dumps(favorite_values["roles"], ensure_ascii=False),
    }
    print(
        "[enterprise_favorite_save] "
        f"event=upsert favorite_id={favorite_id} club_player_id={favorite_values['club_player_id']!r} "
        f"name={favorite_values['name']!r}",
        flush=True,
    )

    if existing:
        db.execute(
            text("""
            UPDATE enterprise_favorite_players
            SET player_id = :player_id,
                player_name = :player_name,
                club_player_id = :club_player_id,
                name = :name,
                nationality = :nationality,
                age = :age,
                potential = :potential,
                form = :form,
                gender = :gender,
                height = :height,
                weight = :weight,
                team = :team,
                league = :league,
                roles_json = CAST(:roles_json AS jsonb)
            WHERE id = :id
              AND user_id = :user_id
            """),
            params,
        )
    else:
        db.execute(
            text("""
            INSERT INTO enterprise_favorite_players (
                id, user_id, player_id, player_name, club_player_id, name, nationality, age,
                potential, form, gender, height, weight, team, league,
                roles_json
            )
            VALUES (
                :id, :user_id, :player_id, :player_name, :club_player_id, :name, :nationality, :age,
                :potential, :form, :gender, :height, :weight, :team, :league,
                CAST(:roles_json AS jsonb)
            )
            """),
            params,
        )

    db.commit()
    row = db.execute(
        text("""
        SELECT efp.id, efp.club_player_id, efp.name, efp.nationality, efp.age, efp.potential, efp.form,
               efp.gender, efp.height, efp.weight, efp.team, efp.league, efp.roles_json,
               pd.metadata->'position_counts' AS position_counts,
               pd.metadata->>'position_count_total' AS position_count_total,
               pd.metadata->'position_names_seen' AS position_names_seen,
               pd.metadata->>'primary_position_code' AS primary_position_code
        FROM enterprise_favorite_players efp
        LEFT JOIN player_data pd ON pd.id = efp.club_player_id
        WHERE efp.id = :id
          AND efp.user_id = :user_id
        """),
        {"id": favorite_id, "user_id": user_id},
    ).mappings().first()
    return _favorite_out(row)



def _resolve_enterprise_player_pool_report_club_row(db: Session, player_payload: Dict[str, Any]) -> Any | None:
    club_player_id = player_payload.get("club_player_id") or player_payload.get("clubPlayerId")
    if club_player_id is not None:
        row = db.execute(
            text("""
            SELECT id, metadata
            FROM player_data
            WHERE id = :player_id
            LIMIT 1
            """),
            {"player_id": club_player_id},
        ).mappings().first()
        if row:
            return row
        print(
            "[enterprise_player_pool_report] "
            f"event=club_id_stale club_player_id={club_player_id!r} name={player_payload.get('name')!r}",
            flush=True,
        )

    player_id = player_payload.get("playerId") or player_payload.get("player_id")
    world_cup_mode = bool(player_payload.get("worldCupMode") or player_payload.get("world_cup_mode"))

    if player_id:
        try:
            return _resolve_club_player_row(db, str(player_id), world_cup_mode)
        except HTTPException:
            if world_cup_mode:
                raise
            print(
                "[enterprise_player_pool_report] "
                f"event=player_id_stale player_id={player_id!r} name={player_payload.get('name')!r}",
                flush=True,
            )

    player_name = str(player_payload.get("name") or "").strip()
    if not player_name:
        return None

    gender = player_payload.get("gender")
    nationality = player_payload.get("nationality")
    age = player_payload.get("age")
    roles = [role for role in (player_payload.get("roles") or []) if role]
    name_tokens = [token for token in re.split(r"\s+", player_name) if token]
    last_name = re.sub(r"[^\wÀ-ž'-]", "", name_tokens[-1]) if name_tokens else ""
    last_name = last_name if last_name and last_name.lower() != player_name.lower() else None
    attempts = [
        ("name_gender_age", {"name": player_name, "gender": gender, "minAge": age, "maxAge": age}),
        ("name_nationality_gender", {"name": player_name, "nationality": nationality, "gender": gender}),
        ("name_nationality", {"name": player_name, "nationality": nationality}),
        ("last_name_gender_age", {"name": last_name, "gender": gender, "minAge": age, "maxAge": age}),
        ("last_name_nationality", {"name": last_name, "nationality": nationality}),
        ("name_role", {"name": player_name, "position": roles[0] if roles else None}),
        ("last_name_role", {"name": last_name, "position": roles[0] if roles else None}),
        ("name_only", {"name": player_name}),
        ("last_name_only", {"name": last_name}),
    ]

    for stage, filters in attempts:
        search_filters = {key: value for key, value in filters.items() if value not in (None, "", [])}
        search_filters["limit"] = 10
        search_filters["worldCupMode"] = False
        rows = search_players(db, search_filters)
        print(
            "[enterprise_player_pool_report] "
            f"event=club_resolve_fallback stage={stage} name={player_name!r} "
            f"nationality={nationality!r} gender={gender!r} matches={len(rows)}",
            flush=True,
        )
        if not rows:
            continue

        for candidate in rows:
            row = db.execute(
                text("""
                SELECT id, metadata
                FROM player_data
                WHERE id = :player_id
                LIMIT 1
                """),
                {"player_id": candidate["id"]},
            ).mappings().first()
            if not row:
                continue
            if not _is_safe_enterprise_club_candidate(dict(row["metadata"] or {}), player_payload, stage):
                print(
                    "[enterprise_player_pool_report] "
                    f"event=club_resolve_rejected_candidate stage={stage} name={player_name!r} "
                    f"candidate_id={row['id']!r}",
                    flush=True,
                )
                continue
            return row

    return None


def _apply_enterprise_club_row_to_report_payload(
    db: Session,
    player_payload: Dict[str, Any],
    club_row: Any,
) -> Dict[str, Any]:
    metadata = dict(club_row["metadata"] or {})
    next_payload = dict(player_payload)
    next_payload["club_player_id"] = int(club_row["id"])
    next_payload["playerId"] = str(club_row["id"])
    next_payload["worldCupMode"] = False
    next_payload["name"] = _metadata_text(metadata, "player_name", "name") or next_payload.get("name")
    next_payload["nationality"] = _metadata_text(metadata, "nationality_name", "nationality") or next_payload.get("nationality")
    next_payload["gender"] = _metadata_text(metadata, "gender") or next_payload.get("gender")
    next_payload["team"] = _metadata_text(metadata, "team_name", "team", "club") or next_payload.get("team")
    next_payload["league"] = _metadata_text(metadata, "league_name", "league") or next_payload.get("league")
    next_payload["age"] = _metadata_int(metadata, "age") or next_payload.get("age")
    next_payload["height"] = _metadata_text(metadata, "height") or next_payload.get("height")
    next_payload["weight"] = _metadata_text(metadata, "weight") or next_payload.get("weight")

    roles = _metadata_roles(metadata)
    if roles:
        next_payload["roles"] = roles

    position_counts = metadata.get("position_counts")
    if isinstance(position_counts, dict):
        next_payload["position_counts"] = position_counts
        next_payload["positionCounts"] = position_counts
    position_names_seen = metadata.get("position_names_seen")
    if isinstance(position_names_seen, list):
        next_payload["position_names_seen"] = position_names_seen
        next_payload["positionNamesSeen"] = position_names_seen
    position_count_total = _metadata_int(metadata, "position_count_total")
    if position_count_total is not None:
        next_payload["position_count_total"] = position_count_total
        next_payload["positionCountTotal"] = position_count_total
    primary_position_code = _metadata_text(metadata, "primary_position_code")
    if primary_position_code:
        next_payload["primary_position_code"] = primary_position_code
        next_payload["primaryPositionCode"] = primary_position_code

    try:
        club_potential = reveal_player_potential(db, club_row["id"], False).get("potential")
        club_form = reveal_player_form(db, club_row["id"], False).get("form")
        next_payload["potential"] = club_potential
        next_payload["form"] = club_form
        if player_payload.get("worldCupMode") or player_payload.get("world_cup_mode"):
            print(
                "[enterprise_player_pool_report] "
                f"event=club_score_override club_player_id={club_row['id']} "
                f"potential={club_potential!r} form={club_form!r}",
                flush=True,
            )
    except Exception as exc:
        print(f"[enterprise_player_pool_report] event=club_score_resolve_failed club_player_id={club_row['id']} error={exc}", flush=True)

    return next_payload


def _enterprise_player_pool_report_cache_key(player_payload: Dict[str, Any]) -> str:
    cache_identity = {
        key: player_payload.get(key)
        for key in (
            "name",
            "gender",
            "nationality",
            "team",
            "league",
            "age",
            "height",
            "weight",
        )
        if player_payload.get(key) is not None
    }
    raw = json.dumps(cache_identity, ensure_ascii=False, sort_keys=True, default=str).lower()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"scoutwise:enterprise-player-pool-report:{raw}"))


def _ensure_enterprise_player_pool_report_scores(
    db: Session,
    player_payload: Dict[str, Any],
) -> Dict[str, Any]:
    next_payload = dict(player_payload)
    score_player_id = next_payload.get("club_player_id") or next_payload.get("clubPlayerId") or next_payload.get("playerId") or next_payload.get("player_id")
    if score_player_id is None:
        return next_payload

    try:
        if next_payload.get("potential") is None:
            next_payload["potential"] = reveal_player_potential(db, score_player_id, False).get("potential")
        if next_payload.get("form") is None:
            next_payload["form"] = reveal_player_form(db, score_player_id, False).get("form")
    except Exception as exc:
        print(
            "[enterprise_player_pool_report] "
            f"event=score_ensure_failed player_id={score_player_id!r} error={exc}",
            flush=True,
        )
    return next_payload


def _get_or_create_enterprise_player_pool_report_from_payload(
    db: Session,
    *,
    user_id: str,
    lang: str,
    version: int,
    player_payload: Dict[str, Any],
    response_favorite_id: str | None = None,
) -> EnterpriseScoutingReportOut:
    if player_payload.get("clubPlayerId") is not None:
        player_payload["club_player_id"] = player_payload.pop("clubPlayerId")

    club_row = _resolve_enterprise_player_pool_report_club_row(db, player_payload)
    if club_row is not None:
        player_payload = _apply_enterprise_club_row_to_report_payload(db, player_payload, club_row)
    elif player_payload.get("worldCupMode"):
        raise HTTPException(status_code=404, detail="Matching club player not found")
    player_payload = _ensure_enterprise_player_pool_report_scores(db, player_payload)

    name = str(player_payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Player name is required")

    cache_key = _enterprise_player_pool_report_cache_key(player_payload)
    outward_favorite_id = response_favorite_id or cache_key
    row = db.execute(
        text("""
        SELECT id, status, content, content_json, language, version, player_payload
        FROM enterprise_player_pool_scouting_reports
        WHERE user_id = :user_id
          AND cache_key = :cache_key
          AND COALESCE(language, 'en') = :lang
          AND version = :version
        LIMIT 1
        """),
        {"user_id": user_id, "cache_key": cache_key, "lang": lang, "version": version},
    ).mappings().first()

    if row:
        if row["status"] == "failed":
            db.execute(text("DELETE FROM enterprise_player_pool_scouting_reports WHERE id = :id"), {"id": row["id"]})
            db.commit()
            row = None
        elif row["status"] == "ready":
            content_json = row["content_json"] if isinstance(row["content_json"], dict) else {}
            player_card = content_json.get("player_card") if isinstance(content_json, dict) else {}
            player_card = player_card if isinstance(player_card, dict) else {}
            missing_requested_score = any(
                player_payload.get(score_key) is not None and player_card.get(score_key) in (None, "")
                for score_key in ("potential", "form")
            )
            if missing_requested_score:
                db.execute(text("DELETE FROM enterprise_player_pool_scouting_reports WHERE id = :id"), {"id": row["id"]})
                db.commit()
                row = None
            else:
                return {
                    "favorite_player_id": outward_favorite_id,
                    "status": row["status"],
                    "content": row["content"],
                    "content_json": row["content_json"],
                    "language": row["language"],
                    "version": row["version"],
                }
        else:
            return {
                "favorite_player_id": outward_favorite_id,
                "status": row["status"],
                "content": row["content"],
                "content_json": row["content_json"],
                "language": row["language"],
                "version": row["version"],
            }

    report_id = str(uuid.uuid4())
    try:
        generated = generate_report_content(
            db,
            favorite_id=cache_key,
            lang=lang,
            version=version,
            player_identity=player_payload,
        )
        db.execute(
            text("""
            INSERT INTO enterprise_player_pool_scouting_reports (
                id, user_id, cache_key, status, language, version,
                player_name, player_payload, content, content_json,
                ready_at, created_at, updated_at
            )
            VALUES (
                :id, :user_id, :cache_key, 'ready', :lang, :version,
                :player_name, CAST(:player_payload AS jsonb), :content, CAST(:content_json AS jsonb),
                NOW(), NOW(), NOW()
            )
            ON CONFLICT (user_id, cache_key, language, version)
            DO UPDATE SET
                status = 'ready',
                player_name = EXCLUDED.player_name,
                player_payload = EXCLUDED.player_payload,
                content = EXCLUDED.content,
                content_json = EXCLUDED.content_json,
                error = NULL,
                ready_at = NOW(),
                updated_at = NOW()
            """),
            {
                "id": report_id,
                "user_id": user_id,
                "cache_key": cache_key,
                "lang": lang,
                "version": version,
                "player_name": name,
                "player_payload": json.dumps(player_payload, ensure_ascii=False, default=str),
                "content": generated["content"],
                "content_json": json.dumps(generated["content_json"], ensure_ascii=False, default=str),
            },
        )
        db.commit()
        return {
            "favorite_player_id": outward_favorite_id,
            "status": "ready",
            "content": generated["content"],
            "content_json": generated["content_json"],
            "language": lang,
            "version": version,
        }
    except Exception as exc:
        db.rollback()
        print(f"[enterprise_player_pool_report] event=failed user_id={user_id} player={name!r} error={exc}", flush=True)
        try:
            db.execute(
                text("""
                INSERT INTO enterprise_player_pool_scouting_reports (
                    id, user_id, cache_key, status, language, version,
                    player_name, player_payload, error, created_at, updated_at
                )
                VALUES (
                    :id, :user_id, :cache_key, 'failed', :lang, :version,
                    :player_name, CAST(:player_payload AS jsonb), :error, NOW(), NOW()
                )
                ON CONFLICT (user_id, cache_key, language, version)
                DO UPDATE SET
                    status = 'failed',
                    player_name = EXCLUDED.player_name,
                    player_payload = EXCLUDED.player_payload,
                    error = EXCLUDED.error,
                    updated_at = NOW()
                """),
                {
                    "id": report_id,
                    "user_id": user_id,
                    "cache_key": cache_key,
                    "lang": lang,
                    "version": version,
                    "player_name": name,
                    "player_payload": json.dumps(player_payload, ensure_ascii=False, default=str),
                    "error": str(exc),
                },
            )
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(status_code=500, detail="Report generation failed") from exc


@app.post("/player-pool/report", response_model=EnterpriseScoutingReportOut)
def create_enterprise_player_pool_report(
    payload: EnterpriseScoutingReportIn,
    user_id: str = Depends(require_auth),
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    lang = normalize_lang(accept_language) or "en"
    version = 7
    player_payload = payload.model_dump(exclude_none=True)
    return _get_or_create_enterprise_player_pool_report_from_payload(
        db,
        user_id=user_id,
        lang=lang,
        version=version,
        player_payload=player_payload,
    )


@app.post("/favorite-players/{favorite_id}/report", response_model=EnterpriseScoutingReportOut)
def get_or_create_enterprise_scouting_report(
    favorite_id: str,
    background_tasks: BackgroundTasks,
    payload: EnterpriseScoutingReportIn = Body(default=EnterpriseScoutingReportIn()),
    user_id: str = Depends(require_auth),
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    lang = normalize_lang(accept_language) or "en"
    version = 7

    favorite_row = _get_owned_enterprise_favorite(db, favorite_id, user_id)
    player_payload = _enterprise_favorite_identity(favorite_row)
    incoming_payload = payload.model_dump(exclude_none=True)
    if incoming_payload.get("clubPlayerId") is not None:
        incoming_payload["club_player_id"] = incoming_payload.pop("clubPlayerId")
    player_payload.update(incoming_payload)

    club_row = _resolve_enterprise_player_pool_report_club_row(db, player_payload)
    if club_row is not None:
        player_payload = _apply_enterprise_club_row_to_report_payload(db, player_payload, club_row)
        try:
            favorite_values = _favorite_values_from_club_row(
                club_row,
                player_payload.get("potential"),
                player_payload.get("form"),
            )
            db.execute(
                text("""
                UPDATE enterprise_favorite_players
                SET player_id = :player_id,
                    player_name = :player_name,
                    club_player_id = :club_player_id,
                    name = :name,
                    nationality = :nationality,
                    age = :age,
                    potential = :potential,
                    form = :form,
                    gender = :gender,
                    height = :height,
                    weight = :weight,
                    team = :team,
                    league = :league,
                    roles_json = CAST(:roles_json AS jsonb)
                WHERE id = :id
                  AND user_id = :user_id
                """),
                {
                    "id": favorite_id,
                    "user_id": user_id,
                    "player_id": str(favorite_values["club_player_id"]) if favorite_values["club_player_id"] is not None else None,
                    "player_name": favorite_values["name"],
                    "club_player_id": favorite_values["club_player_id"],
                    "name": favorite_values["name"],
                    "nationality": favorite_values["nationality"],
                    "age": favorite_values["age"],
                    "potential": favorite_values["potential"],
                    "form": favorite_values["form"],
                    "gender": favorite_values["gender"],
                    "height": favorite_values["height"],
                    "weight": favorite_values["weight"],
                    "team": favorite_values["team"],
                    "league": favorite_values["league"],
                    "roles_json": json.dumps(favorite_values["roles"], ensure_ascii=False),
                },
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            print(
                "[enterprise_report] "
                f"event=favorite_refresh_failed favorite_id={favorite_id} error={exc}",
                flush=True,
            )
    else:
        player_payload = _ensure_enterprise_player_pool_report_scores(db, player_payload)

    return _get_or_create_enterprise_player_pool_report_from_payload(
        db,
        user_id=user_id,
        lang=lang,
        version=version,
        player_payload=player_payload,
        response_favorite_id=favorite_id,
    )


@app.delete("/favorite-players/{favorite_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_enterprise_favorite_player(
    favorite_id: str,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    db.execute(
        text("""
        DELETE FROM enterprise_favorite_players
        WHERE id = :favorite_id
          AND user_id = :user_id
        """),
        {"favorite_id": favorite_id, "user_id": user_id},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
