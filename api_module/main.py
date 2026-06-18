import datetime as dt
import os
import re
import secrets
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

load_dotenv()

from api_module.database import SessionLocal, get_db
from api_module.models import (
    LoginIn,
    LoginOut,
    PasswordResetRequestIn,
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
