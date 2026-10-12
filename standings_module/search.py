"""League identities for standings, without player statistics aggregation."""
from sqlalchemy import text

from league_pool_module.league_pool import _clean_many


def get_league_performance_options(db, leagues=None, countries=None):
    row = db.execute(text("""
        SELECT ARRAY(
            SELECT DISTINCT league_name FROM player_comp_data
            WHERE COALESCE(league_name, '') <> ''
              AND (CAST(:countries AS text[]) = '{}' OR league_country_name = ANY(CAST(:countries AS text[])))
            ORDER BY league_name
        ) AS leagues,
        ARRAY(
            SELECT DISTINCT league_country_name FROM player_comp_data
            WHERE COALESCE(league_country_name, '') <> ''
              AND (CAST(:leagues AS text[]) = '{}' OR league_name = ANY(CAST(:leagues AS text[])))
            ORDER BY league_country_name
        ) AS countries
    """), {'leagues': _clean_many(leagues), 'countries': _clean_many(countries)}).mappings().one()
    return {key: list(row[key] or []) for key in ('leagues', 'countries')}


def search_league_performance(db, leagues=None, countries=None, limit=100):
    rows = db.execute(text("""
        WITH leagues AS (
            SELECT league_id, league_name,
                MAX(league_country_name) AS country_name,
                MAX(league_image_path) AS image_url,
                COUNT(DISTINCT team_id) AS team_count
            FROM player_comp_data
            WHERE league_id IS NOT NULL
              AND (CAST(:leagues AS text[]) = '{}' OR league_name = ANY(CAST(:leagues AS text[])))
              AND (CAST(:countries AS text[]) = '{}' OR league_country_name = ANY(CAST(:countries AS text[])))
            GROUP BY league_id, league_name
            ORDER BY league_name, league_id
            LIMIT :limit
        )
        SELECT l.league_id, l.league_name, l.country_name,
            COALESCE(MAX(eli.image_url), l.image_url) AS image_url,
            CASE WHEN l.league_id IN (2,5,2286) THEN COALESCE(
                (SELECT NULLIF((summary.coverage->>'mainTeamCount')::int, 0)
                 FROM league_season_summaries summary WHERE summary.league_id=l.league_id
                 ORDER BY summary.season_id DESC LIMIT 1), l.team_count)
            ELSE l.team_count END AS team_count
        FROM leagues l
        LEFT JOIN enterprise_league_images eli
          ON eli.league_id = l.league_id AND eli.image_status = 'available'
        GROUP BY l.league_id, l.league_name, l.country_name, l.image_url, l.team_count
        ORDER BY l.league_name, l.league_id
    """), {'leagues': _clean_many(leagues), 'countries': _clean_many(countries),
           'limit': min(max(int(limit), 1), 200)}).mappings().all()
    return [dict(row) for row in rows]
