from datetime import datetime
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
    leagueCountry: Optional[str] = None
    leagueExact: Optional[bool] = False
    team: Optional[str] = None
    teamExact: Optional[bool] = False
    minAge: Optional[float] = Field(default=None, ge=0)
    maxAge: Optional[float] = Field(default=None, ge=0)
    contractStatus: Optional[Literal["loan", "permanent"]] = None
    loanEndDate: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    contractEndDate: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
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


class MatchAnalysisOptionsIn(BaseModel):
    country: Optional[str] = None
    league: Optional[str] = None


class MatchAnalysisOptionsOut(BaseModel):
    countries: List[str]
    leagues: List[str]
    teams: List[str]


class TeamPoolSearchIn(BaseModel):
    team: Optional[str] = None
    country: Optional[str] = None
    league: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=50)


class TeamPoolSearchRow(BaseModel):
    id: str
    name: str
    country: str
    league: str
    logoUrl: Optional[str] = None
    city: str
    coachName: str
    playerCount: int
    stadiumName: str
    stadiumImageUrl: Optional[str] = None
    leagueId: Optional[int] = None


class TeamPlayedMatchRow(BaseModel):
    fixtureId: int
    name: str
    startingAt: str
    country: str
    league: str
    homeTeam: str
    awayTeam: str
    homeTeamId: Optional[int] = None
    awayTeamId: Optional[int] = None
    homeScore: Optional[int] = None
    awayScore: Optional[int] = None
    thisSeason: bool


class MatchAnalysisSearchIn(BaseModel):
    country: Optional[str] = None
    league: Optional[str] = None
    leagueId: Optional[int] = None
    homeTeam: Optional[str] = None
    awayTeam: Optional[str] = None
    startDate: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=50)


class MatchAnalysisSearchOut(BaseModel):
    fixtures: List[Dict[str, Any]]
    pagination: Dict[str, Any]


class EnterpriseFavoriteMatchIn(BaseModel):
    fixture: Dict[str, Any]


class EnterpriseFavoriteMatchOut(BaseModel):
    favoriteId: str
    fixture: Dict[str, Any]
    createdAt: datetime


class EnterpriseMatchReportOut(BaseModel):
    favorite_match_id: str
    status: str
    content_json: Optional[Dict[str, Any]] = None
    language: str
    version: int


class TeamAnalysisReportIn(BaseModel):
    fixtureIds: List[int] = Field(min_length=1, max_length=10)
    teamId: int


class TeamAnalysisReportOut(BaseModel):
    reports: List[Dict[str, Any]]
    teamMetrics: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    perspectives: Dict[str, str] = Field(default_factory=dict)
    playerPerspectives: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    momentumPerspectives: Dict[str, str] = Field(default_factory=dict)
    regionalPerspective: str = ""
    attackProfile: Dict[str, Any] = Field(default_factory=dict)
    defenseProfile: Dict[str, Any] = Field(default_factory=dict)
    scoreFlowProfile: Dict[str, Any] = Field(default_factory=dict)
    overview: List[Dict[str, Any]] = Field(default_factory=list)
    strengths: Dict[str, Any] = Field(default_factory=dict)
    weaknesses: Dict[str, Any] = Field(default_factory=dict)


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


class PlayerCompSeasonSearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=120)
    nationality: Optional[str] = Field(default=None, max_length=120)
    limit: int = Field(default=20, ge=1, le=50)


class PlayerCompSeasonOptionsOut(BaseModel):
    nationalities: List[str] = Field(default_factory=list)


class PlayerCompSeasonCandidateOut(BaseModel):
    playerId: int
    displayName: str
    matchedAlias: str = ""
    nationality: str = ""
    latestTeam: str = ""
    latestPosition: str = ""
    latestSeason: str = ""
    firstSeason: str = ""
    rowCount: int = 0


class PlayerCompSeasonPlayerOut(BaseModel):
    playerId: int
    displayName: str
    imageUrl: Optional[str] = None
    nationality: str = ""
    gender: str = ""
    latestTeam: str = ""
    latestSeason: str = ""


class PlayerCompSeasonRowOut(BaseModel):
    key: str
    playerId: int
    teamId: int
    teamName: str = ""
    leagueId: int
    leagueName: str = ""
    leagueType: str = ""
    leagueSubType: str = ""
    country: str = ""
    leagueShortCode: str = ""
    leagueImagePath: str = ""
    seasonId: int
    seasonName: str = ""
    matchCount: int = 0
    positionName: str = ""
    positionCounts: Dict[str, float] = Field(default_factory=dict)
    age: Optional[int] = None
    height: Optional[float] = None
    weight: Optional[float] = None


class PlayerCompSeasonRowsOut(BaseModel):
    player: PlayerCompSeasonPlayerOut
    rows: List[PlayerCompSeasonRowOut]


class PlayerCompSeasonSourceIn(BaseModel):
    teamId: int
    leagueId: int
    seasonId: int


class PlayerCompSeasonAggregateIn(BaseModel):
    playerId: int
    sources: List[PlayerCompSeasonSourceIn] = Field(min_length=1, max_length=200)


class PlayerCompSeasonAggregateOut(BaseModel):
    playerId: int
    displayName: str
    nationality: str = ""
    gender: str = ""
    age: Optional[int] = None
    height: Optional[float] = None
    weight: Optional[float] = None
    selectedRowCount: int
    matchCount: int
    seasons: List[str] = Field(default_factory=list)
    teams: List[str] = Field(default_factory=list)
    competitions: List[str] = Field(default_factory=list)
    positionCounts: Dict[str, float] = Field(default_factory=dict)
    stats: Dict[str, float] = Field(default_factory=dict)
    selectedRows: List[PlayerCompSeasonRowOut] = Field(default_factory=list)


class EnterpriseFavoritePlayerIn(BaseModel):
    playerId: Optional[str] = None
    sportmonksPlayerId: Optional[int] = None
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
    playerId: Optional[int] = None
    clubPlayerId: Optional[int] = None
    imageUrl: Optional[str] = None
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
