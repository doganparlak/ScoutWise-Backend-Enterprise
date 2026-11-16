"""Searchable summaries of the authenticated user's saved, ready reports."""
from datetime import date
from typing import Literal
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from api_module.database import get_db
from api_module.utilities import require_auth
from player_pool_module.utilities import FOLD_CHAR_MAP_FROM, FOLD_CHAR_MAP_TO, norm_name, player_name_pattern

router = APIRouter()


def folded_report_text(expression):
    """Mirror player-name folding for every report title and search field."""
    folded = f"lower(translate(COALESCE({expression},''),'{FOLD_CHAR_MAP_FROM}','{FOLD_CHAR_MAP_TO}'))"
    for source, target in (('æ','ae'),('œ','oe'),('ß','ss')):
        folded = f"replace({folded},'{source}','{target}')"
    return f"regexp_replace(normalize({folded},NFD),'[\u0300-\u036f]','','g')"


def normalize_report_search(value):
    folded = norm_name(value).replace('æ','ae').replace('œ','oe').replace('ß','ss')
    return ''.join(char for char in unicodedata.normalize('NFD',folded) if not unicodedata.combining(char))


# Only summary fields cross this boundary. Report bodies keep their existing
# owner-scoped opening endpoints, upgrades and modal behaviour.
LIBRARY = """
WITH player_reports AS (
    SELECT r.*, CASE WHEN provider_id ~ '^[0-9]+([.]0+)?$'
        THEN trunc(provider_id::numeric)::bigint ELSE NULL END AS portrait_player_id
    FROM enterprise_player_pool_scouting_reports r
    CROSS JOIN LATERAL (SELECT COALESCE(
        NULLIF(player_payload->>'sportmonksPlayerId',''),NULLIF(player_payload->>'sportmonks_player_id',''),
        NULLIF(player_payload->>'player_id',''),NULLIF(content_json->'player_card'->>'sportmonksPlayerId',''),
        NULLIF(content_json->'player_card'->>'sportmonks_player_id',''),NULLIF(content_json->'player_card'->>'player_id','')) AS provider_id) ids
    WHERE user_id=:uid AND status='ready'
), reports AS (
    SELECT r.id::text AS id, 'player'::text AS kind,
        COALESCE(NULLIF(r.player_name,''), player_payload->>'name', content_json->'player_card'->>'name','') AS title,
        COALESCE(player_payload->>'team',content_json->'player_card'->>'team','') AS subtitle,
        COALESCE(player_payload->>'league',content_json->'player_card'->>'league','') AS competition,
        COALESCE(NULLIF(portrait.image_url,''),NULLIF(named_portrait.image_url,''),
            NULLIF(btrim(player_payload->>'imageUrl'),''),NULLIF(btrim(player_payload->>'image_url'),''),
            NULLIF(btrim(content_json->'player_card'->>'imageUrl'),''),NULLIF(btrim(content_json->'player_card'->>'image_url'),'')) AS image,
        NULL::text AS secondary_image, language, COALESCE(ready_at,r.updated_at,r.created_at) AS saved_at,
        NULL::integer AS match_count
    FROM player_reports r
    LEFT JOIN enterprise_player_images portrait
        ON portrait.player_id=r.portrait_player_id AND portrait.image_status='available'
    LEFT JOIN LATERAL (
        SELECT min(image_url) AS image_url FROM enterprise_player_images image
        WHERE r.portrait_player_id IS NULL AND image.image_status='available'
            AND __IMAGE_NAME__ = __REPORT_NAME__
        HAVING count(DISTINCT image.player_id)=1
    ) named_portrait ON TRUE
    UNION ALL
    SELECT id::text, 'team', COALESCE(team_payload->>'name',''), COALESCE(team_payload->>'country',''),
        COALESCE(team_payload->>'league',''), team_payload->>'logoUrl',NULL,language,
        COALESCE(report_ready_at,updated_at,created_at),cardinality(fixture_ids)
    FROM enterprise_team_reports WHERE user_id=:uid AND report_status='ready' AND report_content IS NOT NULL
    UNION ALL
    SELECT id::text,'coach',COALESCE(coach_payload->>'name',''),COALESCE(coach_payload->>'team',''),
        COALESCE(coach_payload->>'league',''),COALESCE(coach_payload->>'imageUrl',coach_payload->>'image_url'),NULL,language,
        COALESCE(report_ready_at,created_at),NULL
    FROM enterprise_coach_reports WHERE user_id=:uid AND report_status='ready'
    UNION ALL
    SELECT id::text,COALESCE(report_type,'post_match'),
        COALESCE(fixture_payload->'homeTeam'->>'name','') || ' – ' || COALESCE(fixture_payload->'awayTeam'->>'name',''),
        COALESCE(fixture_payload->'season'->>'name',''), COALESCE(fixture_payload->'league'->>'name',''),
        fixture_payload->'homeTeam'->>'imageUrl',fixture_payload->'awayTeam'->>'imageUrl',
        COALESCE(report_content->>'language','en'),COALESCE(report_ready_at,updated_at,created_at),1
    FROM enterprise_favorite_matches WHERE user_id=:uid AND report_status='ready' AND report_content IS NOT NULL
), filtered AS (
    SELECT * FROM reports WHERE (:language='' OR language=:language)
    AND (:search='' OR __SEARCH_TEXT__ LIKE :pattern ESCAPE '\\')
    AND (CAST(:since AS date) IS NULL OR saved_at >= CAST(:since AS date))
    AND (CAST(:until AS date) IS NULL OR saved_at < CAST(:until AS date)+INTERVAL '1 day')
)
"""
LIBRARY = (LIBRARY.replace('__IMAGE_NAME__',folded_report_text('image.player_name'))
           .replace('__REPORT_NAME__',folded_report_text("COALESCE(NULLIF(r.player_name,''),r.player_payload->>'name',r.content_json->'player_card'->>'name')"))
           .replace('__SEARCH_TEXT__',folded_report_text("title || ' ' || subtitle || ' ' || competition")))


def report_summary(row):
    return {'id':row['id'],'kind':row['kind'],'title':row['title'],'subtitle':row['subtitle'],
            'competition':row['competition'],'imageUrl':row['image'],'secondaryImageUrl':row['secondary_image'],
            'language':row['language'],'savedAt':row['saved_at'],'matchCount':row['match_count']}


@router.get('/report-center')
def report_center(
    kind: Literal['all','player','team','coach','pre_match','post_match'] = 'all',
    search: str = Query(default='', max_length=200),
    language: Literal['','tr','en'] = '',
    since: date | None = None,
    until: date | None = None,
    order: Literal['newest','oldest'] = 'newest',
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    user_id: str = Depends(require_auth), db=Depends(get_db),
):
    if since and until and since > until:
        raise HTTPException(422,'Start date must be before end date')
    query = normalize_report_search(search)
    params = {'uid':user_id,'kind':kind,'search':query,'pattern':player_name_pattern(query) or '%',
              'language':language,'since':since,'until':until,'offset':offset,'limit':limit}
    counts = {key:0 for key in ('player','team','coach','pre_match','post_match')}
    for row in db.execute(text(LIBRARY+'SELECT kind,COUNT(*) AS count FROM filtered GROUP BY kind'),params).mappings():
        counts[row['kind']] = row['count']
    # Direction is selected from a closed set, never interpolated user input.
    direction = 'DESC' if order=='newest' else 'ASC'
    rows = db.execute(text(LIBRARY+f"""SELECT * FROM filtered WHERE (:kind='all' OR kind=:kind)
        ORDER BY saved_at {direction},kind,id LIMIT :limit OFFSET :offset"""),params).mappings().all()
    total = sum(counts.values()) if kind=='all' else counts[kind]
    return {'items':[report_summary(row) for row in rows],
            'counts':counts,'total':total,'hasMore':offset+len(rows)<total}
