"""Database-only badge resolution. Never calls SportMonks on a web request."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal
import unicodedata
from pydantic import BaseModel, Field
from sqlalchemy import text


def entity_id(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return int(number) if number.is_finite() and number == number.to_integral_value() and 0 < number <= 9223372036854775807 else None
    except (ValueError, InvalidOperation):
        return None


def folded(value):
    value = str(value or '').strip().casefold().translate(str.maketrans({'ı': 'i', 'ß': 'ss'}))
    return ''.join(c for c in unicodedata.normalize('NFD', value) if not unicodedata.combining(c))


class ImageLookup(BaseModel):
    kind: Literal['team', 'league']
    entityId: int | None = Field(default=None, gt=0, le=9223372036854775807)
    name: str = Field(default='', max_length=200)
    teamId: int | None = Field(default=None, gt=0, le=9223372036854775807)
    playerId: int | None = Field(default=None, gt=0, le=9223372036854775807)
    league: str = Field(default='', max_length=200)
    country: str = Field(default='', max_length=200)


class ImageLookupBatch(BaseModel):
    items: list[ImageLookup] = Field(max_length=100)


def choose_league(item, candidates, contexts):
    """An ID is authoritative; name-only ambiguity must never select an arbitrary badge."""
    if item.entityId:
        return next((row for row in candidates if row['league_id'] == item.entityId), None)
    matches = {row['league_id']: row for row in candidates if folded(row['league_name']) == folded(item.name)}
    if item.country:
        allowed = {row['league_id'] for row in contexts if folded(row['league_country_name']) == folded(item.country)}
        matches = {key: row for key, row in matches.items() if key in allowed}
    if not matches:
        return None
    relevant = [row for row in contexts if row['league_id'] in matches and (not item.teamId or row['team_id'] == item.teamId)]
    if item.playerId:
        player_matches = {row['league_id'] for row in relevant if row['player_id'] == item.playerId}
        if len(player_matches) == 1:
            return matches[next(iter(player_matches))]
        if len(player_matches) > 1:
            return None
    if item.teamId:
        team_matches = {row['league_id'] for row in relevant}
        if len(team_matches) == 1:
            return matches[next(iter(team_matches))]
        if len(team_matches) > 1:
            return None
    return next(iter(matches.values())) if len(matches) == 1 else None


def resolve_images(db, items: list[ImageLookup]):
    if not items:
        return []
    ids = sorted({i.entityId for i in items if i.kind == 'team' and i.entityId})
    names = sorted({i.name.strip().lower() for i in items if i.kind == 'team' and not i.entityId and i.name.strip()})
    teams = db.execute(text('''
        SELECT team_id, team_name, CASE WHEN image_status='available' THEN image_url END AS image_url
        FROM enterprise_team_images
        WHERE team_id = ANY(CAST(:ids AS bigint[])) OR lower(team_name) = ANY(CAST(:names AS text[]))
    '''), {'ids': ids, 'names': names}).mappings().all() if ids or names else []
    leagues, contexts = [], []
    league_items = [i for i in items if i.kind == 'league']
    team_context_items = [i for i in items if i.kind == 'team' and not i.entityId and i.league]
    if league_items or team_context_items:
        # Small catalogue, not the league statistics aggregation.
        leagues = list(db.execute(text('''SELECT league_id, league_name,
            CASE WHEN image_status='available' THEN image_url END AS image_url
            FROM enterprise_league_images''')).mappings())
        team_ids = sorted({i.teamId for i in league_items if i.teamId} | {r['team_id'] for r in teams if any(folded(r['team_name']) == folded(i.name) for i in team_context_items)})
        player_ids = sorted({i.playerId for i in league_items if i.playerId and not i.teamId})
        countries = sorted({i.country for i in league_items if i.country})
        contexts = list(db.execute(text('''
            SELECT DISTINCT player_id, team_id, league_id, league_name, league_country_name, league_image_path
            FROM player_comp_data
            WHERE team_id=ANY(CAST(:teams AS bigint[]))
               OR player_id=ANY(CAST(:players AS bigint[]))
               OR league_country_name=ANY(CAST(:countries AS text[]))
        '''), {'teams': team_ids, 'players': player_ids, 'countries': countries}).mappings()) if team_ids or player_ids or countries else []
        known = {r['league_id'] for r in leagues}
        for row in contexts:
            if row['league_id'] not in known:
                leagues.append({'league_id': row['league_id'], 'league_name': row['league_name'], 'image_url': row['league_image_path']})
                known.add(row['league_id'])
    output = []
    for item in items:
        if item.kind == 'team':
            matches = [r for r in teams if r['team_id'] == item.entityId] if item.entityId else [r for r in teams if folded(r['team_name']) == folded(item.name)]
            if not item.entityId and item.league and len(matches) > 1:
                matching_ids = {r['team_id'] for r in contexts if folded(r['league_name']) == folded(item.league)}
                narrowed = [r for r in matches if r['team_id'] in matching_ids]
                if narrowed:
                    matches = narrowed
            row = matches[0] if len(matches) == 1 else None
            output.append({'entityId': row['team_id'] if row else None, 'name': row['team_name'] if row else item.name, 'imageUrl': row['image_url'] if row else None})
        else:
            row = choose_league(item, leagues, contexts)
            country_matches = {r['league_country_name'] for r in contexts if row and r['league_id'] == row['league_id'] and r['league_country_name']}
            fallback_urls = {r['league_image_path'] for r in contexts if row and r['league_id'] == row['league_id'] and r['league_image_path']}
            url = (row['image_url'] or (next(iter(fallback_urls)) if len(fallback_urls) == 1 else None)) if row else None
            output.append({'entityId': row['league_id'] if row else None, 'name': row['league_name'] if row else item.name, 'country': next(iter(country_matches)) if len(country_matches) == 1 else None, 'imageUrl': url})
    return output


def enrich_player_rows(db, rows):
    # Only enrich the selected response, never an all-candidates ranking pool.
    items, destinations = [], []
    for row in rows:
        content = row.get('content') or {}
        row['content'] = content = dict(content)
        team = entity_id(content.get('team_id'))
        player = entity_id(content.get('player_id'))
        contract = entity_id(content.get('contract_team_id'))
        league = str(content.get('league') or content.get('league_name') or content.get('team_current_league_name') or content.get('latest_domestic_fixture_league_name') or '').strip()
        if not content.get('league_name') and league:
            content['league_name'] = league
        for kind, ident, name, field in [('team', team, content.get('team_name'), 'team_logo_url'), ('team', contract, content.get('contract_team_name'), 'contract_team_logo_url'), ('league', entity_id(content.get('league_id')), league, 'league_logo_url')]:
            if not ident and not name:
                content[field] = None
                continue
            items.append(ImageLookup(kind=kind, entityId=ident, name=str(name or ''), teamId=team, playerId=player))
            destinations.append((content, field))
    for start in range(0, len(items), 100):
        for (content, field), result in zip(destinations[start:start+100], resolve_images(db, items[start:start+100])):
            content[field] = result['imageUrl']
            if field == 'league_logo_url':
                content['league_id'] = result['entityId']
                content['league_country_name'] = result.get('country')
                content['league_resolution'] = 'resolved' if result['entityId'] else 'unresolved'
    for row in rows:
        row['content']['entity_images_resolved'] = True
    return rows
