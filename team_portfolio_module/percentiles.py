"""Position-aware, season-only player benchmarks for Enterprise team reports.

Reuse scouting's exact position eligibility/grouping and percentile rules.
Season values come from saved fixture contributions, never annual averages.
Position distributions use the same stored competition usage as scouting.
"""
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from api_module.database import engine, get_db
from api_module.utilities import require_auth
from report_module.percentiles import MIN_PEERS, RATIOS, NEGATIVE, comparison_groups, rank
from report_module.rate_evidence import empty, add_sample, key as rate_key, rate_value
from scoutwise_pro_module.team_fit import eligible_roles
from standings_module.insights import enqueue_season, SEASON_SUMMARY_VERSION, SCOPED_VERSION
from standings_module.metrics import VERSION, AVERAGES, PLAYER_COUNT_METRICS, numeric
from standings_module.stages import EUROPEAN_LEAGUES

MIN_MINUTES = 90

router = APIRouter()
_ALIASES = {'Successful Crosses (%)': 'Accurate Crosses (%)',
            'On-Target to Goal Conversion (%)': 'On Target Goal Conversion (%)'}
_RATE_LIMITS = {_ALIASES.get(name, name): minimum for name, (_, _, minimum) in RATIOS.items()}


class PlayerReportPercentilesIn(BaseModel):
    teamId: int = Field(gt=0)
    playerId: int = Field(gt=0)
    leagueId: int = Field(gt=0)
    seasonId: int = Field(gt=0)
    seasonName: str = Field(default='', max_length=80)
    scope: Literal['overall', 'qualifying'] = 'overall'
    previewOnly: bool = False

    @model_validator(mode='after')
    def compatible_scope(self):
        if self.scope == 'qualifying' and self.leagueId not in EUROPEAN_LEAGUES:
            raise ValueError('Qualifying scope is only available for European competitions')
        return self


class CacheNotReady(RuntimeError):
    pass


def build_profiles(rows, positions):
    """One player per competition, combining club stints before calculating rates."""
    profiles = {}
    for row in rows:
        for player in row.get('player_contributions') or []:
            pid, tid = int(player['playerId']), int(player['teamId'])
            profile = profiles.setdefault(pid, {'playerId': pid, 'teams': set(), 'minutes': 0,
                'metrics': {}, 'totals': {}, 'rate_inputs': empty(), 'ratings': []})
            profile['teams'].add(tid)
            minutes = numeric(player.get('minutes'))
            if minutes is not None and minutes > 0:
                profile['minutes'] += minutes
            rating = numeric(player.get('rating'))
            if rating is not None and 0 < rating <= 10:
                profile['ratings'].append(rating)
            metrics = player.get('metrics') or {}
            add_sample(profile['rate_inputs'], metrics)
            for name, raw in metrics.items():
                value = numeric(raw)
                if name not in PLAYER_COUNT_METRICS or name in AVERAGES or value is None or value < 0:
                    continue
                total = profile['totals'].setdefault(name, {'sum': 0, 'minutes': 0, 'missingMinutes': False})
                total['sum'] += value
                if minutes is not None and minutes > 0:
                    total['minutes'] += minutes
                else:
                    total['missingMinutes'] = True
    role_counts, genders = defaultdict(lambda: defaultdict(float)), defaultdict(set)
    for row in positions:
        pid = int(row['player_id'])
        if row.get('gender'):
            genders[pid].add(str(row['gender']))
        for role, raw in (row.get('position_counts') or {}).items():
            count = numeric(raw)
            if count is not None and count > 0:
                role_counts[pid][role] += count
    result = {}
    for pid, profile in profiles.items():
        if len(genders[pid]) != 1:
            continue
        roles = eligible_roles({'position_counts': dict(role_counts[pid])})
        if not roles:
            continue
        values = {}
        for name, total in profile['totals'].items():
            if total['minutes'] >= MIN_MINUTES and not total['missingMinutes']:
                values[name] = {'value': total['sum'] * 90 / total['minutes'], 'basis': 'per90',
                                'lowerIsBetter': name in NEGATIVE, 'recordedMinutes': total['minutes']}
        for name, minimum in _RATE_LIMITS.items():
            state = profile['rate_inputs']['rates'].get(rate_key(name))
            value = rate_value(name, state)
            if value is not None and state['denominator'] >= minimum:
                values[name] = {'value': value, 'basis': 'percentage', 'lowerIsBetter': False,
                                'attempts': state['denominator'], 'minimumAttempts': minimum}
        if profile['ratings']:
            values['Rating'] = {'value': sum(profile['ratings']) / len(profile['ratings']), 'basis': 'rating', 'lowerIsBetter': False}
        result[pid] = {'playerId': pid, 'teams': profile['teams'], 'minutes': profile['minutes'],
                       'gender': next(iter(genders[pid])), 'roles': roles,
                       'comparisonGroups': comparison_groups(roles), 'metrics': values}
    return result


@lru_cache(maxsize=4)
def competition_profiles(league, season, scope, published, five_minute_bucket):
    del five_minute_bucket  # Limits reuse of the scouting position metadata to five minutes.
    with engine.connect() as db:
        positions = db.execute(text('''SELECT player_id,gender,position_counts
            FROM player_comp_data WHERE league_id=:league'''), {'league': league}).mappings().all()
        rows = db.execute(text('''SELECT player_contributions,calculation_version,fetched_at
            FROM league_match_contributions WHERE league_id=:league AND season_id=:season
            AND included_in_aggregation=TRUE AND kickoff_at<:cutoff AND (:european=FALSE OR
                (:scope='qualifying' AND coverage->>'scope'='qualifying') OR
                (:scope='overall' AND coverage->>'scope' IN ('league','knockout')))
            ORDER BY kickoff_at,fixture_id''').execution_options(stream_results=True, yield_per=20),
            {'league': league, 'season': season, 'scope': scope, 'european': league in EUROPEAN_LEAGUES,
             'cutoff': datetime.now(timezone.utc)}).mappings()
        def compatible_rows():
            for row in rows:
                if row['calculation_version'] != VERSION or row['fetched_at'] > published:
                    raise CacheNotReady('Fixture refresh has not been published yet')
                yield row
        # Stream compact season contributions without retaining fixture JSON.
        return build_profiles(compatible_rows(), positions)


def player_benchmarks(profiles, team_id, player_id):
    source = profiles.get(player_id)
    if not source or team_id not in source['teams']:
        return {'roles': {}, 'benchmarks': {}, 'selectedGroup': None, 'metrics': {}}
    benchmarks = {}
    for group, info in source['comparisonGroups'].items():
        peers = [profile for profile in profiles.values() if profile['gender'] == source['gender']
                 and profile['minutes'] >= MIN_MINUTES and group in profile['comparisonGroups']]
        metrics = {}
        for name, metric in source['metrics'].items():
            values = sorted(round(peer['metrics'][name]['value'], 6) for peer in peers if name in peer['metrics'])
            value = round(metric['value'], 6)
            percentile = rank(value, values, metric['lowerIsBetter']) if source['minutes'] >= MIN_MINUTES else None
            if percentile is None:
                continue
            position = 1 + sum(other < value if metric['lowerIsBetter'] else other > value for other in values)
            metrics[name] = {**metric, 'season_value': metric['value'], 'percentile': percentile,
                             'peerCount': len(values), 'position': position, 'total_players': len(values)}
        if any(metric['season_value'] != 0 for metric in metrics.values()):
            benchmarks[group] = {**info, 'peerCount': len(peers), 'metrics': metrics}
    selected = next(iter(benchmarks), None)
    return {'roles': source['roles'], 'benchmarks': benchmarks, 'selectedGroup': selected,
            'metrics': benchmarks[selected]['metrics'] if selected else {}}


@router.post('/team-analysis/player-percentiles')
def team_report_player_percentiles(payload: PlayerReportPercentilesIn, user_id: str = Depends(require_auth), db=Depends(get_db)):
    del user_id
    row = db.execute(text('''SELECT status,computed_at,calculation_version,coverage FROM league_season_summaries
        WHERE league_id=:league AND season_id=:season'''), {'league': payload.leagueId, 'season': payload.seasonId}).mappings().first()
    expected = SCOPED_VERSION if payload.leagueId in EUROPEAN_LEAGUES else SEASON_SUMMARY_VERSION
    ready = row is not None and row['computed_at'] is not None and row['calculation_version'] >= expected
    if ready and payload.leagueId in EUROPEAN_LEAGUES:
        ready = payload.scope in ((row['coverage'] or {}).get('stageSummaries') or {})
    base = {'kind': 'percentiles', 'mode': 'per90', 'minimumMinutes': MIN_MINUTES, 'minimumPeers': MIN_PEERS,
            'league_id': payload.leagueId, 'season_id': payload.seasonId,
            'positionSource': 'scouting_competition_usage', 'roles': {}, 'benchmarks': {}, 'metrics': {}, 'selectedGroup': None}
    if not ready:
        if payload.previewOnly:
            return {**base, 'status': 'unavailable'}
        enqueue_season(db, {'leagueId': payload.leagueId, 'seasonId': payload.seasonId, 'seasonName': payload.seasonName})
        return {**base, 'status': 'unavailable' if row and row['status'] == 'failed' else 'pending'}
    published = row['computed_at']
    db.rollback()  # Release the request connection before reading the shared benchmark cache.
    try:
        profiles = competition_profiles(payload.leagueId, payload.seasonId, payload.scope, published,
                                         int(datetime.now(timezone.utc).timestamp()) // 300)
    except CacheNotReady:
        return {**base, 'status': 'pending'}
    return {**base, **player_benchmarks(profiles, payload.teamId, payload.playerId),
            'status': 'ready', 'computed_at': published}
