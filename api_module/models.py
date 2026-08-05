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


class EnterpriseAllowlistEmailIn(BaseModel):
    email: EmailStr
    note: Optional[str] = None


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


class LeaguePoolFilterIn(BaseModel):
    leagues: List[str] = Field(default_factory=list)
    countries: List[str] = Field(default_factory=list)


class LeaguePoolSearchIn(LeaguePoolFilterIn):
    positions: List[str] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=200)


class LeaguePoolFilterOptionsOut(BaseModel):
    leagues: List[str]
    countries: List[str]
    positions: List[str]


class LeaguePoolSearchRow(BaseModel):
    id: str
    content: Dict[str, Any]


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
    player1Sources: List[str] = Field(default_factory=list)
    player2Sources: List[str] = Field(default_factory=list)


class MatchupComparisonSourceOut(BaseModel):
    key: str
    country: str
    leagueShortCode: str
    team: str
    competition: str
    matchCount: float = 0


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
    positionCounts: Dict[str, int] = Field(default_factory=dict)
    positionCountTotal: int = 0
    positionNamesSeen: List[str] = Field(default_factory=list)
    primaryPositionCode: Optional[str] = None
    isOnLoan: Optional[bool] = None
    contractTeamId: Optional[int] = None
    contractTeamName: Optional[str] = None
    loanEndDate: Optional[str] = None
    contractEndDate: Optional[str] = None


class EnterpriseScoutingReportIn(BaseModel):
    playerId: Optional[str] = None
    clubPlayerId: Optional[int] = None
    club_player_id: Optional[int] = None
    worldCupMode: Optional[bool] = False
    name: Optional[str] = None
    gender: Optional[str] = None
    nationality: Optional[str] = None
    team: Optional[str] = None
    league: Optional[str] = None
    age: Optional[int] = None
    height: Optional[str] = None
    weight: Optional[str] = None
    potential: Optional[int] = Field(default=None, ge=0, le=100)
    form: Optional[int] = Field(default=None, ge=0, le=100)
    roles: List[str] = Field(default_factory=list)
    positionCounts: Dict[str, int] = Field(default_factory=dict)
    positionCountTotal: int = 0
    positionNamesSeen: List[str] = Field(default_factory=list)
    primaryPositionCode: Optional[str] = None


class EnterpriseScoutingReportOut(BaseModel):
    favorite_player_id: str
    status: str
    content: Optional[str] = None
    content_json: Optional[Dict[str, Any]] = None
    language: str
    version: int


class EnterpriseTacticBoardIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    formation: str = Field(default="4-3-3", min_length=1, max_length=40)
    board_data: Dict[str, Any] = Field(default_factory=dict)


class EnterpriseTacticBoardOut(EnterpriseTacticBoardIn):
    id: str
    createdAt: str
    updatedAt: str


class EnterpriseDashboardNoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=600)


class EnterpriseDashboardNotePatchIn(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=600)
    isDone: Optional[bool] = None


class EnterpriseDashboardNoteOut(BaseModel):
    id: str
    text: str
    isDone: bool
    createdAt: str
    updatedAt: str


class EnterpriseDashboardReportOut(BaseModel):
    id: str
    playerName: Optional[str] = None
    playerTeam: Optional[str] = None
    team: Optional[str] = None
    league: Optional[str] = None
    nationality: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[str] = None
    potential: Optional[int] = None
    form: Optional[int] = None
    roles: List[str] = Field(default_factory=list)
    positionCounts: Dict[str, int] = Field(default_factory=dict)
    positionCountTotal: int = 0
    positionNamesSeen: List[str] = Field(default_factory=list)
    primaryPositionCode: Optional[str] = None
    status: str
    language: str
    version: int
    createdAt: str
    updatedAt: str
    readyAt: Optional[str] = None


class EnterpriseProStrategyIn(BaseModel):
    strategy: str = Field(default="", max_length=6000)


class EnterpriseProStrategyOut(EnterpriseProStrategyIn):
    updatedAt: Optional[str] = None


class EnterpriseProStrategySavedIn(EnterpriseProStrategyIn):
    strategyName: str = Field(default="Default Strategy", min_length=1, max_length=120)


class EnterpriseProStrategySavedOut(EnterpriseProStrategySavedIn):
    id: str
    createdAt: str
    updatedAt: str


class EnterpriseProChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = "default"
    strategy: Optional[str] = None
    tutorial_mode: Optional[bool] = False
