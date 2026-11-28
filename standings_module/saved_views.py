"""Account-owned league table layouts."""
import json

from sqlalchemy import text


def list_views(db, user_id):
    rows = db.execute(text("""SELECT id, name, settings FROM enterprise_league_saved_views
        WHERE user_id=:user_id ORDER BY lower(name), id"""), {'user_id': user_id}).mappings()
    return [{**row['settings'], 'id': row['id'], 'name': row['name']} for row in rows]


def save_view(db, user_id, name, settings):
    team = settings['tab'] == 'teams'
    if (team and (settings['preset'] == 'general' or settings['mode'] == 'per90')) or (
        not team and (settings['preset'] in {'standings', 'passing', 'xg'} or settings['columns'] == [])
    ):
        raise ValueError('Invalid table layout')
    if any(not column or len(column) > 100 for column in settings['columns'] or []):
        raise ValueError('Invalid column')
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {'key': f'league-saved-views:{user_id}'})
    params = {'user_id': user_id, 'name': name, 'tab': settings['tab'], 'settings': json.dumps(settings)}
    existing = db.execute(text("""SELECT id FROM enterprise_league_saved_views
        WHERE user_id=:user_id AND settings->>'tab'=:tab AND lower(name)=lower(:name)"""), params).scalar()
    if existing is None:
        count = db.execute(text("SELECT count(*) FROM enterprise_league_saved_views WHERE user_id=:user_id"), params).scalar_one()
        if count >= 30:
            raise ValueError('Saved view limit reached')
        view_id = db.execute(text("""INSERT INTO enterprise_league_saved_views (user_id,name,settings)
            VALUES (:user_id,:name,CAST(:settings AS jsonb)) RETURNING id"""), params).scalar_one()
    else:
        view_id = existing
        db.execute(text("""UPDATE enterprise_league_saved_views SET name=:name,settings=CAST(:settings AS jsonb),updated_at=now()
            WHERE user_id=:user_id AND id=:id"""), {**params, 'id': view_id})
    db.commit()
    return {**settings, 'id': view_id, 'name': name}


def delete_view(db, user_id, view_id):
    db.execute(text("DELETE FROM enterprise_league_saved_views WHERE user_id=:user_id AND id=:id"),
               {'user_id': user_id, 'id': view_id})
    db.commit()
