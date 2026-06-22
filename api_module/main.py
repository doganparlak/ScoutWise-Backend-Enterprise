import datetime as dt
import json
import os
import re
import secrets
import uuid
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query as FastAPIQuery, Response, status
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
from potential_form_module.form import reveal_player_form
from potential_form_module.potential import reveal_player_potential

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
}


def msg(key: str, lang: str | None) -> str:
    preferred = normalize_lang(lang) or "tr"
    return MESSAGES[key][preferred]

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


@app.on_event("startup")
def ensure_enterprise_sessions_table() -> None:
    db = SessionLocal()
    try:
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
        db.commit()
    finally:
        db.close()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}


@app.post("/auth/signup")
def signup(
    payload: SignUpIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not PASSWORD_RE.match(payload.password):
        preferred = normalize_lang(payload.uiLanguage) or normalize_lang(accept_language) or "tr"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=msg("weak_pw", preferred),
        )

    email_norm = payload.email.strip()
    preferred = normalize_lang(payload.uiLanguage) or normalize_lang(accept_language) or "tr"

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
        preferred = normalize_lang(accept_language) or "tr"
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
    raw_roles = metadata.get("roles") or metadata.get("positions")
    if isinstance(raw_roles, list):
        roles = [str(role).strip() for role in raw_roles if str(role).strip()]
        if roles:
            return roles
    position = _metadata_text(metadata, "position_name", "position", "role")
    return [position] if position else []


def _favorite_out(row: Any) -> EnterpriseFavoritePlayerOut:
    data = row._mapping if hasattr(row, "_mapping") else row
    roles_raw = data.get("roles_json") or []
    if isinstance(roles_raw, str):
        try:
            roles_raw = json.loads(roles_raw)
        except json.JSONDecodeError:
            roles_raw = []
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
    )


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
          AND (:age IS NULL OR COALESCE(metadata->>'age', '') = CAST(:age AS text))
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
    if not row:
        raise HTTPException(status_code=404, detail="Matching club player not found")
    return row


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


@app.get("/favorite-players", response_model=list[EnterpriseFavoritePlayerOut])
def list_enterprise_favorite_players(
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
        SELECT id, club_player_id, name, nationality, age, potential, form,
               gender, height, weight, team, league, roles_json, created_at
        FROM enterprise_favorite_players
        WHERE user_id = :user_id
        ORDER BY created_at DESC
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
    club_row = _resolve_club_player_row(db, payload.playerId, bool(payload.worldCupMode))
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
        "player_id": str(favorite_values["club_player_id"]),
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
        SELECT id, club_player_id, name, nationality, age, potential, form,
               gender, height, weight, team, league, roles_json
        FROM enterprise_favorite_players
        WHERE id = :id
          AND user_id = :user_id
        """),
        {"id": favorite_id, "user_id": user_id},
    ).mappings().first()
    return _favorite_out(row)


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
