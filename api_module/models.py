from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class SignUpIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    uiLanguage: Optional[Literal["en", "tr"]] = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str
    uiLanguage: Optional[Literal["en", "tr"]] = None


class LoginOut(BaseModel):
    token: str
    user: Dict[str, Any]


class SignupCodeRequestIn(BaseModel):
    email: EmailStr


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class VerifyResetIn(BaseModel):
    email: EmailStr
    code: str


class VerifySignupIn(BaseModel):
    email: EmailStr
    code: str


class SetNewPasswordIn(BaseModel):
    email: EmailStr
    new_password: str


class ProfileOut(BaseModel):
    id: str
    email: EmailStr
    uiLanguage: Literal["en", "tr"]
    isEmailVerified: bool
