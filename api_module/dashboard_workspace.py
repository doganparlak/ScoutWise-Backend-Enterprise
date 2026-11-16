"""Account-owned dashboard activity and compact portfolio summaries."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from api_module.report_center import LIBRARY, report_summary

router = APIRouter()
CATALOG = LIBRARY.split(', filtered AS (',1)[0]


def ensure_dashboard_workspace(db):
    db.execute(text((Path(__file__).resolve().parent.parent/'migrations/20261116_dashboard_workspace.sql').read_text()))


class ReportReference(BaseModel):
    kind: Literal['player','team','coach','pre_match','post_match']
    id: UUID


class PinReference(ReportReference):
    pinned: bool


def load_report_reference(db,user_id,kind,report_id):
    row=db.execute(text(CATALOG+' SELECT * FROM reports WHERE kind=:kind AND id=:id'),
                   {'uid':user_id,'kind':kind,'id':str(report_id)}).mappings().first()
    if row is None:
        raise HTTPException(404,'Saved report not found')
    return report_summary(row)


@router.get('/report-center/item')
def report_center_item(kind: Literal['player','team','coach','pre_match','post_match'], id: UUID,
                       user_id: str=Depends(require_auth),db=Depends(get_db)):
    return load_report_reference(db,user_id,kind,id)


def saved_references(db,user_id,refs):
    if not refs:
        return []
    rows=db.execute(text(CATALOG+''' SELECT r.*,ref.value->>'openedAt' AS opened_at
        FROM jsonb_array_elements(CAST(:refs AS jsonb)) WITH ORDINALITY ref(value,position)
        JOIN reports r ON r.kind=ref.value->>'kind' AND r.id=ref.value->>'id'
        ORDER BY ref.position'''),{'uid':user_id,'refs':json.dumps(refs)}).mappings().all()
    return [{**report_summary(row),**({'openedAt':row['opened_at']} if row['opened_at'] else {})} for row in rows]


def record_ready_report_open(db,user_id,kind,report_id):
    """Called only after a saved report's ownership and ready state were checked."""
    reference={'kind':kind,'id':str(report_id),'openedAt':datetime.now(timezone.utc).isoformat()}
    try:
        db.execute(text('''INSERT INTO enterprise_dashboard_workspace(user_id,recent_reports)
            VALUES (:uid,jsonb_build_array(CAST(:reference AS jsonb)))
            ON CONFLICT(user_id) DO UPDATE SET recent_reports=
                jsonb_build_array(CAST(:reference AS jsonb)) || (
                  SELECT COALESCE(jsonb_agg(previous.value ORDER BY previous.position),'[]'::jsonb)
                  FROM (SELECT value,position FROM jsonb_array_elements(enterprise_dashboard_workspace.recent_reports)
                        WITH ORDINALITY old(value,position)
                        WHERE NOT(value->>'kind'=:kind AND value->>'id'=:id)
                        ORDER BY position LIMIT 11) previous), updated_at=now()'''),
            {'uid':user_id,'reference':json.dumps(reference),'kind':kind,'id':str(report_id)})
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f'[dashboard_workspace] event=recent_open_deferred error_type={type(exc).__name__}')


@router.get('/dashboard/workspace')
def workspace(user_id: str=Depends(require_auth),db=Depends(get_db)):
    state=db.execute(text('SELECT pinned_reports,recent_reports FROM enterprise_dashboard_workspace WHERE user_id=:uid'),{'uid':user_id}).mappings().first() or {}
    counts=dict(db.execute(text('''SELECT
        (SELECT count(*) FROM enterprise_favorite_players WHERE user_id=:uid) AS players,
        (SELECT count(*) FROM enterprise_favorite_teams WHERE user_id=:uid) AS teams,
        (SELECT count(*) FROM enterprise_favorite_coaches WHERE user_id=:uid) AS coaches,
        (SELECT count(DISTINCT fixture_id) FROM enterprise_favorite_matches WHERE user_id=:uid) AS matches,
        (SELECT count(*) FROM enterprise_tactic_boards WHERE user_id=:uid) AS tactics'''),{'uid':user_id}).mappings().one())
    latest=db.execute(text(CATALOG+' SELECT * FROM reports ORDER BY saved_at DESC,kind,id LIMIT 5'),{'uid':user_id}).mappings().all()
    ready_counts={row['kind']:row['count'] for row in db.execute(text(CATALOG+' SELECT kind,count(*) AS count FROM reports GROUP BY kind'),{'uid':user_id}).mappings()}
    # These are durable statuses, not estimated completion percentages. Stale
    # processing rows must not be presented as actively running background jobs.
    activity=db.execute(text('''WITH activity AS (
        SELECT id::text,'player' AS kind,COALESCE(player_name,'') AS title,status,updated_at FROM enterprise_player_pool_scouting_reports WHERE user_id=:uid
        UNION ALL SELECT id::text,'team',team_payload->>'name',report_status,updated_at FROM enterprise_team_reports WHERE user_id=:uid
        UNION ALL SELECT id::text,'coach',coach_payload->>'name',report_status,updated_at FROM enterprise_coach_reports WHERE user_id=:uid
        UNION ALL SELECT id::text,COALESCE(report_type,'post_match'),
            COALESCE(fixture_payload->'homeTeam'->>'name','')||' – '||COALESCE(fixture_payload->'awayTeam'->>'name',''),report_status,updated_at
            FROM enterprise_favorite_matches WHERE user_id=:uid
    ) SELECT *,CASE WHEN status='processing' AND updated_at<now()-interval '30 minutes' THEN 'interrupted'
                    WHEN status IN ('failed','error') THEN 'failed' ELSE 'processing' END AS display_status
      FROM activity WHERE status IN ('processing','failed','error') AND updated_at>=now()-interval '7 days'
      ORDER BY (status='processing' AND updated_at>=now()-interval '30 minutes') DESC,updated_at DESC,id LIMIT 6'''),{'uid':user_id}).mappings().all()
    fixtures=db.execute(text('''WITH saved AS (
        SELECT DISTINCT ON (fixture_id) fixture_id,fixture_payload,starting_at,
            upper(COALESCE(NULLIF(state_code,''),fixture_payload->'state'->>'code','')) AS state
        FROM enterprise_favorite_matches WHERE user_id=:uid ORDER BY fixture_id,updated_at DESC,id
    ), chosen AS (
        (SELECT *, 'upcoming' AS category FROM saved WHERE starting_at>=now() AND state IN ('NS','TBA','NOT_STARTED','SCHEDULED') ORDER BY starting_at,fixture_id LIMIT 4)
        UNION ALL
        (SELECT *, 'completed' AS category FROM saved WHERE state IN ('FT','AET','FT_PEN','AWARDED','WO') ORDER BY starting_at DESC,fixture_id DESC LIMIT 4)
    ) SELECT chosen.*,COALESCE((SELECT jsonb_agg(jsonb_build_object('id',m.id,'kind',COALESCE(m.report_type,'post_match'),'status',m.report_status) ORDER BY m.updated_at DESC)
        FROM enterprise_favorite_matches m WHERE m.user_id=:uid AND m.fixture_id=chosen.fixture_id),'[]'::jsonb) AS reports
      FROM chosen ORDER BY category DESC,CASE WHEN category='upcoming' THEN starting_at END,CASE WHEN category='completed' THEN starting_at END DESC'''),{'uid':user_id}).mappings().all()
    return {'counts':counts,'readyReports':sum(ready_counts.values()),'readyCounts':ready_counts,
            'latest':[report_summary(row) for row in latest],
            'recent':saved_references(db,user_id,state.get('recent_reports') or []),
            'pinned':saved_references(db,user_id,state.get('pinned_reports') or []),
            'activity':[{'id':r['id'],'kind':r['kind'],'title':r['title'],'status':r['display_status'],'updatedAt':r['updated_at']} for r in activity],
            'fixtures':[{'fixture':r['fixture_payload'],'category':r['category'],'reports':r['reports']} for r in fixtures]}


def update_reference(db,user_id,body,pin=False):
    if not pin or body.pinned:
        load_report_reference(db,user_id,body.kind,body.id)
    db.execute(text('INSERT INTO enterprise_dashboard_workspace(user_id) VALUES (:uid) ON CONFLICT(user_id) DO NOTHING'),{'uid':user_id})
    field='pinned_reports' if pin else 'recent_reports'
    saved=db.execute(text(f'SELECT {field} FROM enterprise_dashboard_workspace WHERE user_id=:uid FOR UPDATE'),{'uid':user_id}).scalar_one()
    reference={'kind':body.kind,'id':str(body.id)}
    existing=[r for r in saved if r.get('kind')!=body.kind or r.get('id')!=str(body.id)]
    if pin:
        if body.pinned:
            if len(existing)>=8:
                raise HTTPException(422,'You can pin up to eight reports')
            existing=[reference,*existing]
    else:
        existing=[{**reference,'openedAt':datetime.now(timezone.utc).isoformat()},*existing][:12]
    db.execute(text(f'UPDATE enterprise_dashboard_workspace SET {field}=CAST(:refs AS jsonb),updated_at=now() WHERE user_id=:uid'),{'uid':user_id,'refs':json.dumps(existing)})
    db.commit()
    return {'pinned':saved_references(db,user_id,existing)} if pin else {'ok':True}


@router.get('/dashboard/workspace/pins')
def report_pins(user_id: str=Depends(require_auth),db=Depends(get_db)):
    refs=db.execute(text('SELECT pinned_reports FROM enterprise_dashboard_workspace WHERE user_id=:uid'),{'uid':user_id}).scalar_one_or_none() or []
    return {'pinned':saved_references(db,user_id,refs)}


@router.put('/dashboard/workspace/pins')
def pin_report(body: PinReference,user_id: str=Depends(require_auth),db=Depends(get_db)):
    return update_reference(db,user_id,body,pin=True)


@router.post('/dashboard/workspace/recent')
def recent_report(body: ReportReference,user_id: str=Depends(require_auth),db=Depends(get_db)):
    return update_reference(db,user_id,body)
