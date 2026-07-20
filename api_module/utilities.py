from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import random
import re
import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from fastapi import Header, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from api_module.database import SessionLocal
from constants_module.constants import ROLE_SHORT_TO_LONG

PASSWORD_ITERATIONS = int(os.getenv("PASSWORD_ITERATIONS", "210000"))
CODE_EXPIRY_MINUTES = int(os.getenv("EMAIL_CODE_EXPIRY_MINUTES", "10"))
CODE_HASH_SECRET = os.getenv("CODE_HASH_SECRET", os.getenv("SMTP_APP_PASSWORD", ""))

settings: Dict[str, Any] = {
    "email": {
        "sender_email": os.environ.get("SMTP_SENDER_EMAIL", ""),
        "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
        "sender_password": os.environ.get("SMTP_APP_PASSWORD", ""),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
    }
}

IMG_TAG = re.compile(r'<img[^>]+src="([^"]+)"[^>]*>', re.IGNORECASE)
HTMLY_RE = re.compile(r'</?(table|thead|tbody|tr|td|th|ul|ol|li|div|p|h[1-6]|span)\b', re.IGNORECASE)

def normalize_lang(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if v in {"en", "tr"}:
        return v
    if v.startswith("en"):
        return "en"
    if v.startswith("tr"):
        return "tr"
    if "english" in v:
        return "en"
    if "türkçe" in v or "turkish" in v:
        return "tr"
    return None


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def get_db() -> Session:
    return SessionLocal()


def append_chat_message(db: Session, session_token: str, role: str, content: str) -> None:
    db.execute(
        text(
            """
            INSERT INTO enterprise_pro_chat_messages (session_token, role, content, created_at)
            VALUES (:token, :role, :content, NOW())
            """
        ),
        {"token": session_token, "role": role, "content": content},
    )
    db.commit()


def delete_chat_messages(db: Session, session_token: str) -> None:
    db.execute(
        text("DELETE FROM enterprise_pro_chat_messages WHERE session_token = :token"),
        {"token": session_token},
    )
    db.commit()


def load_chat_messages(db: Session, session_token: str) -> List[Dict[str, str]]:
    rows = db.execute(
        text(
            """
            SELECT role, content
            FROM enterprise_pro_chat_messages
            WHERE session_token = :token
            ORDER BY id ASC
            """
        ),
        {"token": session_token},
    ).mappings().all()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def session_exists_and_active(db: Session, session_id: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1
            FROM enterprise_pro_chat_sessions
            WHERE token = :token
              AND ended_at IS NULL
            """
        ),
        {"token": session_id},
    ).first()
    return row is not None


def get_session_language(db: Session, token: str) -> Optional[str]:
    row = db.execute(
        text(
            """
            SELECT language
            FROM enterprise_pro_chat_sessions
            WHERE token = :token
              AND ended_at IS NULL
            """
        ),
        {"token": token},
    ).mappings().first()
    return row["language"] if row and row["language"] else None


def split_response_parts(html: str):
    parts = []
    pos = 0
    html = html or ""

    for match in IMG_TAG.finditer(html):
        start, end = match.start(), match.end()
        if start > pos:
            chunk = html[pos:start].strip()
            if chunk:
                parts.append({"type": "html" if HTMLY_RE.search(chunk) else "text", "html": chunk})
        parts.append({"type": "image", "src": match.group(1)})
        pos = end

    if pos < len(html):
        tail = html[pos:].strip()
        if tail:
            parts.append({"type": "html" if HTMLY_RE.search(tail) else "text", "html": tail})

    return parts


def hash_pw(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_pw(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
    except ValueError:
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)


def user_row_to_dict(row: Any) -> dict:
    data = row._mapping if hasattr(row, "_mapping") else row
    return {
        "id": str(data["id"]),
        "email": data["email"],
        "uiLanguage": data.get("ui_language") or "tr",
        "isEmailVerified": bool(data.get("is_email_verified")),
        "createdAt": data.get("created_at").isoformat()
        if data.get("created_at") is not None
        else None,
    }


def get_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing/invalid Authorization")
    return authorization.split(" ", 1)[1].strip()


def require_auth(authorization: Optional[str] = Header(None)) -> str:
    token = get_bearer_token(authorization)
    db = SessionLocal()
    try:
        row = db.execute(
            text(
                """
                SELECT user_id
                FROM enterprise_sessions
                WHERE token = :token
                  AND revoked_at IS NULL
                  AND expires_at > NOW()
                """
            ),
            {"token": token},
        ).mappings().first()
    finally:
        db.close()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid token")
    return str(row["user_id"])


def revoke_session(db: Session, token: str) -> None:
    db.execute(
        text("UPDATE enterprise_sessions SET revoked_at = NOW() WHERE token = :token"),
        {"token": token},
    )
    db.commit()


def _gen_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def _hash_code(email: str, code: str, purpose: str) -> str:
    material = f"{email.strip().lower()}:{purpose}:{code}:{CODE_HASH_SECRET}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def create_email_code(
    db: Session,
    *,
    user_id: str,
    email: str,
    purpose: str,
) -> str:
    code = _gen_code()
    code_hash = _hash_code(email, code, purpose)
    expires_at = now_utc() + dt.timedelta(minutes=CODE_EXPIRY_MINUTES)

    table = _code_table_for_purpose(purpose)
    db.execute(
        text(
            f"""
            UPDATE {table}
            SET consumed_at = NOW()
            WHERE lower(email) = lower(:email)
              AND consumed_at IS NULL
            """
        ),
        {"email": email.strip()},
    )
    db.execute(
        text(
            f"""
            INSERT INTO {table} (user_id, email, code_hash, expires_at, created_at)
            VALUES (:user_id, :email, :code_hash, :expires_at, NOW())
            """
        ),
        {
            "user_id": user_id,
            "email": email.strip(),
            "code_hash": code_hash,
            "expires_at": expires_at,
        },
    )
    db.commit()
    return code


def verify_email_code(
    db: Session,
    *,
    email: str,
    code: str,
    purpose: str,
) -> Optional[str]:
    table = _code_table_for_purpose(purpose)
    code_hash = _hash_code(email, code, purpose)
    row = db.execute(
        text(
            f"""
            SELECT id, user_id
            FROM {table}
            WHERE lower(email) = lower(:email)
              AND code_hash = :code_hash
              AND consumed_at IS NULL
              AND expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"email": email.strip(), "code_hash": code_hash},
    ).mappings().first()

    if not row:
        return None

    db.execute(
        text(f"UPDATE {table} SET consumed_at = NOW() WHERE id = :id"),
        {"id": row["id"]},
    )
    db.commit()
    return str(row["user_id"])


def _code_table_for_purpose(purpose: str) -> str:
    if purpose == "signup":
        return "enterprise_email_verification_codes"
    if purpose == "reset":
        return "enterprise_password_reset_codes"
    raise ValueError(f"Unsupported email code purpose: {purpose}")


def send_email_code(
    receiver_email: str,
    code: str,
    mail_type: str,
    lang: Optional[str] = None,
) -> None:
    se = settings["email"]["sender_email"]
    spw = settings["email"]["sender_password"]
    host = settings["email"]["smtp_server"]
    port = settings["email"]["smtp_port"]

    if not (se and spw and host and port):
        return

    is_tr = normalize_lang(lang) == "tr"
    if mail_type == "signup":
        subject = (
            "ScoutWise Enterprise e-posta doğrulama kodun"
            if is_tr
            else "Your ScoutWise Enterprise verification code"
        )
        body = (
            "Merhaba,\n\n"
            "ScoutWise Enterprise hesabını doğrulamak için aşağıdaki 6 haneli kodu kullan:\n\n"
            f"{code}\n\n"
            f"Kod {CODE_EXPIRY_MINUTES} dakika içinde geçerliliğini yitirir.\n\n"
            "Sevgiler,\nScoutWise Support"
        ) if is_tr else (
            "Dear User,\n\n"
            "Use this 6-digit code to verify your ScoutWise Enterprise account:\n\n"
            f"{code}\n\n"
            f"The code expires in {CODE_EXPIRY_MINUTES} minutes.\n\n"
            "Best,\nScoutWise Support"
        )
    else:
        subject = (
            "ScoutWise Enterprise parola sıfırlama kodun"
            if is_tr
            else "Your ScoutWise Enterprise password reset code"
        )
        body = (
            "Merhaba,\n\n"
            "Parolanı sıfırlamak için aşağıdaki 6 haneli kodu kullan:\n\n"
            f"{code}\n\n"
            f"Kod {CODE_EXPIRY_MINUTES} dakika içinde geçerliliğini yitirir.\n\n"
            "Sevgiler,\nScoutWise Support"
        ) if is_tr else (
            "Dear User,\n\n"
            "Use this 6-digit code to reset your password:\n\n"
            f"{code}\n\n"
            f"The code expires in {CODE_EXPIRY_MINUTES} minutes.\n\n"
            "Best,\nScoutWise Support"
        )

    msg = MIMEMultipart()
    msg["From"] = se
    msg["To"] = receiver_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    server = None
    try:
        server = smtplib.SMTP(host, port)
        server.starttls()
        server.login(se, spw)
        server.sendmail(se, receiver_email, msg.as_string())
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
