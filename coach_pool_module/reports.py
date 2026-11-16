"""Durable, user-owned coach analyses. Section requests reuse saved results."""
import hashlib
import json
from threading import Lock
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from . import analysis, comparison, tactics, profiles, momentum, regional, score_flow
from .coach_pool import CoachPoolSearchIn, search_coaches
from .progressive import progressive_response

router = APIRouter()
VERSION = 1
_locks = [Lock() for _ in range(64)]


class ReportIn(BaseModel):
    coachId: int = Field(gt=0)
    periodIds: list[str] = Field(min_length=1, max_length=1000)
    language: str = Field(pattern='^(tr|en)$')


class SectionIn(BaseModel):
    section: str
    payload: dict = Field(default_factory=dict)


def section_key(section, payload):
    body = {k: v for k, v in payload.items() if k != 'stream'}
    if 'periodIds' in body:
        body['periodIds'] = sorted(set(body['periodIds']))
    if 'slots' in body:
        body['slots'] = [{'periodIds': sorted(set(s['periodIds']))} for s in body['slots']]
    return section + ':' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def report_out(row):
    return {'id': str(row['id']), 'coach': row['coach_payload'], 'history': row['history_payload'],
            'periodIds': row['period_ids'], 'language': row['language'], 'status': row['report_status'],
            'error': row['report_error'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at']}


def load_report(db, user_id, report_id):
    row = db.execute(text('SELECT * FROM enterprise_coach_reports WHERE id=:id AND user_id=:uid'),
                     {'id': str(report_id), 'uid': user_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, 'Coach report not found')
    return row


@router.get('/coach-reports')
def list_reports(coach_id: int = Query(gt=0), language: str | None = None,
                 user_id: str = Depends(require_auth), db=Depends(get_db)):
    rows = db.execute(text('''SELECT * FROM enterprise_coach_reports WHERE user_id=:uid AND coach_id=:cid
        AND (:lang IS NULL OR language=:lang) ORDER BY created_at DESC, id DESC LIMIT 100'''),
        {'uid': user_id, 'cid': coach_id, 'lang': language}).mappings().all()
    return [report_out(row) for row in rows]


@router.get('/coach-reports/{report_id}')
def get_report(report_id: UUID, user_id: str = Depends(require_auth), db=Depends(get_db)):
    row=load_report(db,user_id,report_id)
    if row['report_status']=='ready':
        from api_module.dashboard_workspace import record_ready_report_open
        record_ready_report_open(db,user_id,'coach',report_id)
    return report_out(row)


@router.post('/coach-reports')
def create_report(body: ReportIn, user_id: str = Depends(require_auth), db=Depends(get_db)):
    ids = sorted(set(body.periodIds))
    selection = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
    # Reopen the original snapshot even if upstream career coverage has since changed.
    existing = db.execute(text('''SELECT * FROM enterprise_coach_reports WHERE user_id=:uid AND coach_id=:cid
        AND selection_key=:key AND language=:lang AND version=:version'''),
        {'uid': user_id, 'cid': body.coachId, 'key': selection, 'lang': body.language, 'version': VERSION}).mappings().one_or_none()
    if existing:
        coach = existing['coach_payload']
    else:
        history = analysis.get_periods(db, body.coachId)
        groups = tactics.resolve_periods(history, ids)
        periods = [p for rows in groups.values() for p in rows]
        if len({fid for p in periods for fid in p['fixtureIds']}) < history['minimumMatches']:
            raise HTTPException(422, 'Not enough eligible matches for a coach report')
        if len({(p['teamId'], p.get('season') or p.get('seasonId')) for p in periods}) > 5:
            raise HTTPException(422, 'Select at most five team-seasons')
        candidates = search_coaches(db, CoachPoolSearchIn(coachId=body.coachId))
        if not candidates:
            raise HTTPException(404, 'Coach profile not found')
        coach = candidates[0]
    # Portfolio membership and report creation commit together.
    db.execute(text('''INSERT INTO enterprise_favorite_coaches(user_id, coach_id, coach_payload)
        VALUES (:uid,:cid,CAST(:coach AS jsonb)) ON CONFLICT(user_id,coach_id) DO UPDATE
        SET coach_payload=EXCLUDED.coach_payload, updated_at=now()'''),
        {'uid': user_id, 'cid': body.coachId, 'coach': json.dumps(coach)})
    if not existing:
        db.execute(text('''INSERT INTO enterprise_coach_reports
            (user_id,coach_id,selection_key,language,version,coach_payload,history_payload,period_ids)
            VALUES (:uid,:cid,:key,:lang,:version,CAST(:coach AS jsonb),CAST(:history AS jsonb),CAST(:ids AS jsonb))
            ON CONFLICT(user_id,coach_id,selection_key,language,version) DO NOTHING'''),
            {'uid': user_id, 'cid': body.coachId, 'key': selection, 'lang': body.language, 'version': VERSION,
             'coach': json.dumps(coach), 'history': json.dumps(history), 'ids': json.dumps(ids)})
    db.commit()
    row = db.execute(text('''SELECT * FROM enterprise_coach_reports WHERE user_id=:uid AND coach_id=:cid
        AND selection_key=:key AND language=:lang AND version=:version'''),
        {'uid': user_id, 'cid': body.coachId, 'key': selection, 'lang': body.language, 'version': VERSION}).mappings().one()
    return report_out(row)


def save_section(db, row, key, value):
    required = [section_key(name, {'periodIds': row['period_ids']}) for name in ('tactics', 'profiles')]
    db.execute(text('''UPDATE enterprise_coach_reports SET
        report_content = report_content || CAST(:value AS jsonb), updated_at=now(),
        report_status=CASE WHEN (report_content || CAST(:value AS jsonb)) ? :tactics
                          AND (report_content || CAST(:value AS jsonb)) ? :profiles THEN 'ready' ELSE report_status END,
        report_ready_at=CASE WHEN (report_content || CAST(:value AS jsonb)) ? :tactics
                          AND (report_content || CAST(:value AS jsonb)) ? :profiles THEN COALESCE(report_ready_at,now()) ELSE report_ready_at END,
        report_error=NULL
        WHERE id=:id AND user_id=:uid'''),
        {'value': json.dumps({key: value}), 'id': row['id'], 'uid': row['user_id'],
         'tactics': required[0], 'profiles': required[1]})
    db.commit()


@router.post('/coach-reports/{report_id}/sections')
def report_section(report_id: UUID, body: SectionIn, user_id: str = Depends(require_auth), db=Depends(get_db)):
    functions = {
        'comparison': (comparison.ComparisonRequest, comparison.compare_coach_scopes),
        'momentum': (momentum.MomentumRequest, momentum.coach_momentum),
        'regional': (regional.RegionalRequest, regional.coach_regional),
        'score-flow': (score_flow.ScoreFlowRequest, score_flow.coach_score_flow),
        'momentum/availability': (None, momentum.coach_momentum_availability),
        'regional/availability': (None, regional.coach_regional_availability),
        'score-flow/availability': (None, score_flow.coach_score_flow_availability),
        'tactics': (tactics.TacticsRequest, None), 'profiles': (tactics.TacticsRequest, None),
    }
    if body.section not in functions:
        raise HTTPException(422, 'Unknown coach report section')
    model, function = functions[body.section]
    try:
        parsed = model.model_validate(body.payload) if model else None
    except ValidationError:
        raise HTTPException(422, 'Invalid coach report section selection') from None
    payload = parsed.model_dump(exclude={'stream'}) if parsed else {}
    key = section_key(body.section, payload)
    row = load_report(db, user_id, report_id)
    cached = row['report_content'].get(key)
    if cached is not None:
        return cached

    def generate():
        with _locks[int(hashlib.sha256((str(report_id) + key).encode()).hexdigest()[:8], 16) % len(_locks)]:
            current = load_report(db, user_id, report_id)
            if key in current['report_content']:
                return current['report_content'][key]
            token = analysis.report_history.set(row['history_payload'])
            try:
                if body.section in ('tactics', 'profiles'):
                    build = tactics.build_tactics if body.section == 'tactics' else profiles.build_profiles
                    value = build(db, row['coach_id'], row['history_payload'], parsed.periodIds)
                elif parsed:
                    value = function(parsed, coach_id=row['coach_id'], user_id=user_id, db=db)
                else:
                    value = function(coach_id=row['coach_id'], user_id=user_id, db=db)
                save_section(db, row, key, value)
                return value
            except Exception:
                db.rollback()
                if body.section in ('tactics', 'profiles'):
                    db.execute(text("""UPDATE enterprise_coach_reports SET report_status='failed',
                        report_error='Report section generation failed. Reopen the report to retry.', updated_at=now()
                        WHERE id=:id AND user_id=:uid AND report_status <> 'ready'"""),
                        {'id': row['id'], 'uid': user_id})
                    db.commit()
                raise
            finally:
                analysis.report_history.reset(token)

    if body.section in ('tactics', 'profiles') and body.payload.get('stream'):
        build = tactics.build_tactics if body.section == 'tactics' else profiles.build_profiles
        first = build(db, row['coach_id'], row['history_payload'], parsed.periodIds, include_ai=False)
        return progressive_response(first, generate)
    return generate()
