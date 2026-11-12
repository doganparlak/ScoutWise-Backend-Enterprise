"""Position-aware, season-only player benchmarks for Enterprise team reports.

Reuse scouting's exact position eligibility/grouping and percentile rules.
Season values come from saved fixture contributions, never annual averages.
Position distributions use the same stored competition usage as scouting.
"""
from collections import defaultdict
from datetime import datetime, timezone
import json
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from api_module.database import benchmark_engine, get_db
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


def build_profiles(rows, positions, emit=None):
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
    for pid in list(profiles):
        profile = profiles.pop(pid)
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
        item = {'playerId': pid, 'teams': profile['teams'], 'minutes': profile['minutes'],
                       'gender': next(iter(genders[pid])), 'roles': roles,
                       'comparisonGroups': comparison_groups(roles), 'metrics': values}
        if emit is None:
            result[pid] = item
        else:
            emit(item)
    return result


SEASON_BENCHMARK_CACHE_VERSION = 1


def database_player_benchmarks(league, season, scope, published, team_id, player_id):
    params = {'league': league, 'season': season, 'scope': scope, 'published': published,
              'european': league in EUROPEAN_LEAGUES, 'version': VERSION,
              'profile_version': SEASON_BENCHMARK_CACHE_VERSION,
              'cutoff': datetime.now(timezone.utc)}
    selection = """league_id=:league AND season_id=:season AND included_in_aggregation=TRUE
        AND kickoff_at<:cutoff AND (:european=FALSE OR
        (:scope='qualifying' AND coverage->>'scope'='qualifying') OR
        (:scope='overall' AND coverage->>'scope' IN ('league','knockout')))"""
    with benchmark_engine.begin() as cache_db:
        def inputs():
            revision = cache_db.execute(text('SELECT revision FROM public.scoutwise_report_source_revision_v1 WHERE singleton')).scalar_one()
            state = cache_db.execute(text("""SELECT COALESCE(bool_and(calculation_version=:version
                AND fetched_at IS NOT NULL AND fetched_at<=:published),true) AS compatible,
                md5(COALESCE(string_agg(fixture_id::text||':'||calculation_version::text||':'||
                    extract(epoch FROM fetched_at)::text||':'||COALESCE(source_fingerprint,''),',' ORDER BY fixture_id),'')) AS fingerprint
                FROM league_match_contributions WHERE """ + selection), params).mappings().one()
            if not state['compatible']:
                raise CacheNotReady('Fixture refresh has not been published yet')
            return {**params, 'position_revision': revision, 'fingerprint': state['fingerprint']}

        key = inputs()
        def ready():
            return cache_db.execute(text("""SELECT 1 FROM public.scoutwise_team_player_meta_v1
                WHERE league_id=:league AND season_id=:season AND scope=:scope
                AND published_at=:published AND position_revision=:position_revision
                AND contribution_fingerprint=:fingerprint AND profile_version=:profile_version"""), key).scalar() is not None
        if not ready():
            cache_db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:lock,0))'),
                             {'lock': f'scoutwise-team-player-profiles:{league}:{season}:{scope}'})
            key = inputs()
            if not ready():
                positions = cache_db.execute(text('SELECT player_id,gender,position_counts FROM player_comp_data WHERE league_id=:league'), params).mappings().all()
                rows = cache_db.execute(text("""SELECT player_contributions,calculation_version,fetched_at
                    FROM league_match_contributions WHERE """ + selection + ' ORDER BY kickoff_at,fixture_id')
                    .execution_options(stream_results=True, yield_per=20), params).mappings()
                def compatible_rows():
                    for row in rows:
                        if row['calculation_version'] != VERSION or row['fetched_at'] > published:
                            raise CacheNotReady('Fixture refresh has not been published yet')
                        yield row
                cache_db.execute(text('DELETE FROM public.scoutwise_team_player_meta_v1 WHERE league_id=:league AND season_id=:season AND scope=:scope'), params)
                cache_db.execute(text("""INSERT INTO public.scoutwise_team_player_meta_v1
                    VALUES(:league,:season,:scope,:published,:position_revision,:fingerprint,:profile_version)"""), key)
                batch = []
                def flush():
                    if not batch:
                        return
                    cache_db.execute(text("""INSERT INTO public.scoutwise_team_player_profiles_v1
                        SELECT :league,:season,:scope,(p->>'playerId')::bigint,p-'_group_order',
                         ARRAY(SELECT jsonb_array_elements_text(p->'_group_order'))
                        FROM jsonb_array_elements(CAST(:profiles AS jsonb)) p"""),
                        {**params, 'profiles': json.dumps(batch, allow_nan=False)})
                    batch.clear()
                def emit(profile):
                    batch.append({**profile, 'teams': sorted(profile['teams']),
                                  '_group_order': list(profile['comparisonGroups'])})
                    if len(batch) >= 64:
                        flush()
                try:
                    build_profiles(compatible_rows(), positions, emit=emit)
                    flush()
                finally:
                    rows.close()
                cache_db.execute(text('SELECT public.scoutwise_build_team_player_distributions_v1(:league,:season,:scope)'), params)
        return cache_db.execute(text('SELECT public.scoutwise_team_player_percentiles_v1(:league,:season,:scope,:team,:player)'),
                                {**params, 'team': team_id, 'player': player_id}).scalar_one()


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
        benchmarks = database_player_benchmarks(payload.leagueId, payload.seasonId, payload.scope,
                                                 published, payload.teamId, payload.playerId)
    except CacheNotReady:
        return {**base, 'status': 'pending'}
    return {**base, **benchmarks,
            'status': 'ready', 'computed_at': published}
