from typing import Any, Dict, List, Literal, Optional

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


class PlayerPoolSearchIn(BaseModel):
    name: Optional[str] = None
    gender: Optional[Literal["male", "female"]] = None
    nationality: Optional[str] = None
    nationalityExact: Optional[bool] = False
    league: Optional[str] = None
    leagueExact: Optional[bool] = False
    team: Optional[str] = None
    teamExact: Optional[bool] = False
    minAge: Optional[float] = Field(default=None, ge=0)
    maxAge: Optional[float] = Field(default=None, ge=0)
    minHeight: Optional[float] = Field(default=None, ge=0)
    maxHeight: Optional[float] = Field(default=None, ge=0)
    minWeight: Optional[float] = Field(default=None, ge=0)
    maxWeight: Optional[float] = Field(default=None, ge=0)
    position: Optional[str] = None
    limit: Optional[int] = Field(default=100, ge=1, le=200)
    worldCupMode: Optional[bool] = False


class PlayerPoolSearchRow(BaseModel):
    id: str | int
    content: Dict[str, Any]


class PlayerPoolWeeklyPopularIn(BaseModel):
    limit: Optional[int] = Field(default=10, ge=1, le=10)
    worldCupMode: Optional[bool] = False


class PlayerPoolFilterOptionsOut(BaseModel):
    teams: List[str]
    leagues: List[str]
    nationalities: List[str]
    positions: List[str]


class PlayerPoolPotentialOut(BaseModel):
    player_id: str
    status: str
    potential: int = Field(ge=0, le=100)
    source: str


class PlayerPoolFormOut(BaseModel):
    player_id: str
    status: str
    form: int = Field(ge=0, le=100)
    source: str


class MatchupComparisonIn(BaseModel):
    player1Id: str
    player2Id: str
    worldCupMode: Optional[bool] = False


class MatchupComparisonPlayer(BaseModel):
    id: str | int
    content: Dict[str, Any]


class MatchupComparisonOut(BaseModel):
    player1: MatchupComparisonPlayer
    player2: MatchupComparisonPlayer


class EnterpriseFavoritePlayerIn(BaseModel):
    playerId: Optional[str] = None
    name: Optional[str] = None
    nationality: Optional[str] = None
    age: Optional[int] = None
    potential: Optional[int] = Field(default=None, ge=0, le=100)
    form: Optional[int] = Field(default=None, ge=0, le=100)
    gender: Optional[str] = None
    height: Optional[int | str] = None
    weight: Optional[int | str] = None
    team: Optional[str] = None
    league: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    worldCupMode: Optional[bool] = False


class EnterpriseFavoritePlayerOut(BaseModel):
    id: str
    clubPlayerId: Optional[int] = None
    name: str
    nationality: Optional[str] = None
    age: Optional[int] = None
    potential: Optional[int] = Field(default=None, ge=0, le=100)
    form: Optional[int] = Field(default=None, ge=0, le=100)
    gender: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[str] = None
    team: Optional[str] = None
    league: Optional[str] = None
    roles: List[str] = Field(default_factory=list)


class EnterpriseScoutingReportIn(BaseModel):
    name: Optional[str] = None
    gender: Optional[str] = None
    nationality: Optional[str] = None
    team: Optional[str] = None
    age: Optional[int] = None
    height: Optional[str] = None
    weight: Optional[str] = None
    potential: Optional[int] = Field(default=None, ge=0, le=100)
    form: Optional[int] = Field(default=None, ge=0, le=100)
    clubPlayerId: Optional[int] = None


class EnterpriseScoutingReportOut(BaseModel):
    favorite_player_id: str
    status: str
    content: Optional[str] = None
    content_json: Optional[Dict[str, Any]] = None
    language: str
    version: int


class EnterpriseLineupSlotPlayer(BaseModel):
    id: str
    favoriteId: Optional[str] = None
    name: str
    age: Optional[int] = None
    nationality: Optional[str] = None
    league: Optional[str] = None
    team: Optional[str] = None
    role: Optional[str] = None
    gender: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[str] = None
    potential: Optional[int] = Field(default=None, ge=0, le=100)
    form: Optional[int] = Field(default=None, ge=0, le=100)
    traits: List[str] = Field(default_factory=list)


class EnterpriseLineupSlot(BaseModel):
    slotId: str
    slotLabel: str
    row: int
    orderIndex: int
    player: Optional[EnterpriseLineupSlotPlayer] = None


class EnterpriseLineupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    formation: str = Field(min_length=1, max_length=40)
    teamRating: float = Field(default=0, ge=0, le=100)
    slots: List[EnterpriseLineupSlot] = Field(default_factory=list)


class EnterpriseLineupOut(EnterpriseLineupIn):
    id: str
    createdAt: str
    updatedAt: str


class EnterpriseProStrategyIn(BaseModel):
    strategy: str = Field(default="", max_length=6000)


class EnterpriseProStrategyOut(EnterpriseProStrategyIn):
    updatedAt: Optional[str] = None


class EnterpriseProChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = "default"
    strategy: Optional[str] = None
    tutorial_mode: Optional[bool] = False
