"""Competition-season ranks for team report data views; no match or AI fetching."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from api_module.database import get_db
from api_module.utilities import require_auth
from standings_module.comparison_ranks import rank_metrics
from standings_module.pre_match_player_ranks import cached_player_ranks
from standings_module.stages import EUROPEAN_LEAGUES
from standings_module.insights import enqueue_season, SEASON_SUMMARY_VERSION, SCOPED_VERSION

router = APIRouter()


class TeamReportRankingsIn(BaseModel):
    teamId: int = Field(gt=0)
    leagueId: int = Field(gt=0)
    seasonId: int = Field(gt=0)
    seasonName: str = Field(default='', max_length=80)
    scope: Literal['overall', 'qualifying'] = 'overall'
    entity: Literal['team', 'player'] = 'team'
    playerId: int | None = Field(default=None, gt=0)
    mode: Literal['perMatch', 'per90', 'total'] = 'perMatch'

    @model_validator(mode='after')
    def compatible_basis(self):
        if self.entity == 'player' and (self.playerId is None or self.mode == 'perMatch'):
            raise ValueError('Player ranks require a player ID and per90 or total mode')
        if self.entity == 'team' and self.mode == 'per90':
            raise ValueError('Team ranks support perMatch and total mode')
        if self.scope == 'qualifying' and self.leagueId not in EUROPEAN_LEAGUES:
            raise ValueError('Qualifying scope is only available for European competitions')
        return self


@router.post('/team-analysis/rankings')
def team_report_rankings(payload: TeamReportRankingsIn, user_id: str = Depends(require_auth), db=Depends(get_db)):
    del user_id  # Sports rankings are shared; this endpoint returns no saved/private reports.
    params = {'league': payload.leagueId, 'season': payload.seasonId}
    row = db.execute(text('''SELECT status,computed_at,calculation_version,team_metrics,coverage
        FROM league_season_summaries WHERE league_id=:league AND season_id=:season'''), params).mappings().first()
    expected = SCOPED_VERSION if payload.leagueId in EUROPEAN_LEAGUES else SEASON_SUMMARY_VERSION
    ready = row is not None and row['computed_at'] is not None and row['calculation_version'] >= expected
    snapshot = dict(row['team_metrics'] or {}) if ready else {}
    if ready and payload.leagueId in EUROPEAN_LEAGUES:
        scoped = ((row['coverage'] or {}).get('stageSummaries') or {}).get(payload.scope)
        ready = bool(scoped)
        snapshot = ((scoped or {}).get('season') or {}).get('team_metrics') or {}
    if not ready:
        enqueue_season(db, {'leagueId': payload.leagueId, 'seasonId': payload.seasonId, 'seasonName': payload.seasonName})
        return {'status': 'unavailable' if row and row['status'] == 'failed' else 'pending', 'metrics': {}, 'mode': payload.mode}
    if payload.entity == 'team':
        ranks = rank_metrics(snapshot, payload.mode).get(str(payload.teamId), {})
    else:
        ranks = cached_player_ranks(payload.leagueId, payload.seasonId, payload.scope,
                                    int(datetime.now(timezone.utc).timestamp()) // 60, payload.mode)
        ranks = ranks.get((payload.teamId, payload.playerId), {})
    return {'status': 'ready', 'metrics': ranks, 'mode': payload.mode,
            'computed_at': row['computed_at'], 'league_id': payload.leagueId, 'season_id': payload.seasonId}
