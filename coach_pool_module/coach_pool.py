"""Database-only coach search. Sportmonks is used only by the explicit sync command."""
from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import text

from match_analysis_module.current_leagues import load_current_teams, fold


class CoachPoolSearchIn(BaseModel):
    coach: str = Field(default='', max_length=120)
    coachId: int | None = Field(default=None, gt=0)
    teamId: int | None = Field(default=None, gt=0)
    leagueId: int | None = Field(default=None, gt=0)


class CoachPoolRow(BaseModel):
    id: str
    coachId: int
    name: str
    imageUrl: str | None = None
    nationality: str
    nationalityImageUrl: str | None = None
    dateOfBirth: str
    age: int | None = None
    teamId: int | None = None
    team: str
    teamImageUrl: str | None = None
    leagueId: int | None = None
    league: str
    country: str
    appointmentStart: str
    appointmentEnd: str


def load_coach_profiles(db, filters, team_ids):
    if (filters.teamId or filters.leagueId) and not team_ids:
        return []
    # LIKE metacharacters in coach names are literal; accents are normalized at import.
    query = fold(filters.coach).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    records = db.execute(text("""SELECT coach_name, image_url, image_status, profile_data
        FROM public.enterprise_coach_images
        WHERE (:coach_id IS NULL OR coach_id = :coach_id)
          AND (:query = '' OR search_text LIKE :pattern ESCAPE '\\')
          AND (:filter_teams = false OR EXISTS (
              SELECT 1 FROM jsonb_array_elements(COALESCE(profile_data->'teams', '[]'::jsonb)) appointment
              WHERE appointment->>'active' = 'true'
                AND appointment->>'team_id' = ANY(CAST(:team_ids AS text[]))
          ))
        ORDER BY coach_name, coach_id LIMIT 501
    """), {'coach_id': filters.coachId, 'query': query, 'pattern': f'%{query}%',
            'filter_teams': bool(filters.teamId or filters.leagueId),
            'team_ids': [str(tid) for tid in team_ids]}).mappings().all()
    if len(records) > 500:
        raise ValueError('Too many coaches matched. Please narrow your search.')
    profiles = []
    for record in records:
        profile = dict(record['profile_data'])
        # The directory columns are authoritative for names and images.
        profile['display_name'] = record['coach_name']
        profile['image_path'] = record['image_url'] if record['image_status'] == 'available' else None
        profiles.append(profile)
    return profiles


def _row(coach, appointment, team, context):
    birthday = coach.get('date_of_birth') or ''
    try:
        born = date.fromisoformat(birthday)
        today = date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    except ValueError:
        age = None
    nationality = coach.get('nationality') or coach.get('country') or {}
    image = coach.get('image_path')
    return {
        'id': f"{coach['id']}:{team.get('id') or 0}", 'coachId': coach['id'],
        'name': coach.get('display_name') or coach.get('name') or coach.get('common_name') or '',
        'imageUrl': image if image and 'placeholder' not in image.lower() else None,
        'nationality': nationality.get('name') or '', 'nationalityImageUrl': nationality.get('image_path'),
        'dateOfBirth': birthday, 'age': age, 'teamId': team.get('id'), 'team': team.get('name') or '',
        'teamImageUrl': team.get('image_path'), 'leagueId': context.get('league_id'),
        'league': context.get('league_name') or '', 'country': context.get('league_country_name') or '',
        'appointmentStart': appointment.get('start') or '', 'appointmentEnd': appointment.get('end') or '',
    }


def search_coaches(db, filters: CoachPoolSearchIn):
    query = filters.coach.strip()
    if not (filters.coachId or filters.teamId or filters.leagueId or len(query) >= 2):
        raise ValueError('Select a team or league, or enter at least two characters of a coach name.')
    contexts = {int(t['team_id']): t for t in load_current_teams(db)}
    team_ids = [tid for tid, context in contexts.items()
                if (not filters.teamId or tid == filters.teamId)
                and (not filters.leagueId or context['league_id'] == filters.leagueId)]
    coaches = load_coach_profiles(db, filters, team_ids)
    result = []
    for coach in coaches:
        active = [a for a in coach.get('teams') or [] if a.get('active') and a.get('team_id')]
        if not active and not filters.teamId and not filters.leagueId:
            result.append(_row(coach, {}, {}, {}))
        for appointment in active:
            tid = int(appointment['team_id'])
            if filters.teamId and tid != filters.teamId:
                continue
            context = contexts.get(tid) or {}
            if filters.leagueId and context.get('league_id') != filters.leagueId:
                continue
            team = appointment.get('team') or {'id': tid, 'name': context.get('team_name') or ''}
            result.append(_row(coach, appointment, team, context))
    unique = {row['id']: row for row in result}
    return sorted(unique.values(), key=lambda row: (fold(row['name']), fold(row['team'])))
