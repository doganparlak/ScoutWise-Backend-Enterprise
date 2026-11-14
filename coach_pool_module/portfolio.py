"""Authenticated, user-owned coach portfolios."""
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from coach_pool_module.coach_pool import CoachPoolRow, CoachPoolSearchIn, search_coaches

router = APIRouter()


class FavoriteCoachIn(BaseModel):
    coach: CoachPoolRow


def favorite_out(row):
    return {'favoriteId': str(row['id']), 'coach': row['coach_payload'],
            'createdAt': row['created_at'], 'updatedAt': row['updated_at']}


@router.get('/favorite-coaches')
def list_coaches(user_id: str = Depends(require_auth), db=Depends(get_db)):
    rows = db.execute(text('''SELECT id, coach_payload, created_at, updated_at
        FROM enterprise_favorite_coaches WHERE user_id=:user_id
        ORDER BY created_at DESC, id'''), {'user_id': user_id}).mappings().all()
    return [favorite_out(row) for row in rows]


@router.post('/favorite-coaches')
def create_coach(payload: FavoriteCoachIn, user_id: str = Depends(require_auth), db=Depends(get_db)):
    if payload.coach.coachId <= 0:
        raise HTTPException(422, 'Invalid coach ID')
    candidates = search_coaches(db, CoachPoolSearchIn(coachId=payload.coach.coachId))
    coach = next((row for row in candidates if row['id'] == payload.coach.id), None)
    if not coach:
        raise HTTPException(404, 'Coach profile not found')
    row = db.execute(text('''INSERT INTO enterprise_favorite_coaches (user_id, coach_id, coach_payload)
        VALUES (:user_id, :coach_id, CAST(:payload AS jsonb))
        ON CONFLICT (user_id, coach_id) DO UPDATE SET
            coach_payload=EXCLUDED.coach_payload, updated_at=now()
        RETURNING id, coach_payload, created_at, updated_at'''),
        {'user_id': user_id, 'coach_id': coach['coachId'], 'payload': json.dumps(coach)}).mappings().one()
    result = favorite_out(row)
    db.commit()
    return result


@router.delete('/favorite-coaches/{favorite_id}', status_code=204)
def delete_coach(favorite_id: UUID, user_id: str = Depends(require_auth), db=Depends(get_db)):
    deleted = db.execute(text('''DELETE FROM enterprise_favorite_coaches
        WHERE id=:id AND user_id=:user_id'''), {'id': str(favorite_id), 'user_id': user_id}).rowcount
    if not deleted:
        raise HTTPException(404, 'Saved coach not found')
    db.commit()
