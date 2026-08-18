from __future__ import annotations

import os
import unicodedata
from datetime import date, timedelta
from typing import Any

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session


SPORTMONKS_BASE_URL = os.getenv(
    "SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3/football"
).rstrip("/")
MAX_DATE_RANGE_DAYS = 100
MAX_UPSTREAM_PAGES = 5
MAX_FIXTURE_RESULTS = 50


class SportMonksError(RuntimeError):
    pass


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _fold(value: str | None) -> str:
    translated = _clean(value).translate(
        str.maketrans({"ı": "i", "İ": "I", "ğ": "g", "Ğ": "G", "ş": "s", "Ş": "S", "ç": "c", "Ç": "C", "ü": "u", "Ü": "U", "ö": "o", "Ö": "O", "ß": "ss"})
    )
    return "".join(
        char for char in unicodedata.normalize("NFD", translated)
        if unicodedata.category(char) != "Mn"
    ).casefold()


def _matches(actual: str | None, requested: str | None) -> bool:
    needle = _fold(requested)
    return not needle or needle in _fold(actual)


def _team_matches(actual: str | None, requested: str | None) -> bool:
    needle = _fold(requested)
    return not needle or _fold(actual) == needle


def get_match_filter_options(
    db: Session,
    country: str | None = None,
    league: str | None = None,
) -> dict[str, list[str]]:
    country = _clean(country)
    league = _clean(league)
    row = db.execute(
        text(
            """
            SELECT
              ARRAY(
                SELECT DISTINCT league_country_name
                FROM player_comp_data
                WHERE COALESCE(league_country_name, '') <> ''
                  AND (:league = '' OR league_name = :league)
                ORDER BY league_country_name
              ) AS countries,
              ARRAY(
                SELECT DISTINCT league_name
                FROM player_comp_data
                WHERE COALESCE(league_name, '') <> ''
                  AND (:country = '' OR league_country_name = :country)
                ORDER BY league_name
              ) AS leagues,
              ARRAY(
                SELECT DISTINCT team_name
                FROM player_comp_data
                WHERE COALESCE(team_name, '') <> ''
                  AND (:country = '' OR league_country_name = :country)
                  AND (:league = '' OR league_name = :league)
                ORDER BY team_name
              ) AS teams
            """
        ),
        {"country": country, "league": league},
    ).mappings().one()
    return {key: list(row[key] or []) for key in ("countries", "leagues", "teams")}


def resolve_league_id(db: Session, league: str | None, country: str | None) -> int | None:
    league = _clean(league)
    if not league:
        return None
    value = db.execute(
        text(
            """
            SELECT league_id
            FROM player_comp_data
            WHERE league_name = :league
              AND (:country = '' OR league_country_name = :country)
            GROUP BY league_id
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """
        ),
        {"league": league, "country": _clean(country)},
    ).scalar()
    return int(value) if value is not None else None


def _current_score(scores: list[dict[str, Any]], location: str) -> int | None:
    current = next(
        (
            item for item in scores
            if item.get("description") == "CURRENT"
            and (item.get("score") or {}).get("participant") == location
        ),
        None,
    )
    value = (current or {}).get("score", {}).get("goals")
    return int(value) if isinstance(value, (int, float)) else None


def _fixture_out(fixture: dict[str, Any]) -> dict[str, Any] | None:
    participants = fixture.get("participants") or []
    home = next((team for team in participants if (team.get("meta") or {}).get("location") == "home"), None)
    away = next((team for team in participants if (team.get("meta") or {}).get("location") == "away"), None)
    if not home or not away:
        return None
    league = fixture.get("league") or {}
    country = league.get("country") or {}
    state = fixture.get("state") or {}
    scores = fixture.get("scores") or []
    return {
        "fixtureId": fixture.get("id"),
        "name": fixture.get("name") or f'{home.get("name", "")} vs {away.get("name", "")}',
        "startingAt": fixture.get("starting_at"),
        "resultInfo": fixture.get("result_info"),
        "country": {"id": country.get("id"), "name": country.get("name") or "", "imageUrl": country.get("image_path")},
        "league": {"id": league.get("id"), "name": league.get("name") or "", "imageUrl": league.get("image_path")},
        "homeTeam": {"id": home.get("id"), "name": home.get("name") or "", "imageUrl": home.get("image_path"), "score": _current_score(scores, "home")},
        "awayTeam": {"id": away.get("id"), "name": away.get("name") or "", "imageUrl": away.get("image_path"), "score": _current_score(scores, "away")},
        "state": {"code": state.get("short_name") or state.get("state") or "", "name": state.get("name") or ""},
    }


def search_fixtures(filters: dict[str, Any]) -> dict[str, Any]:
    start_value = _clean(filters.get("startDate"))
    end_value = _clean(filters.get("endDate"))
    if not start_value and not end_value:
        end = date.today()
        start = end - timedelta(days=MAX_DATE_RANGE_DAYS)
    elif start_value and not end_value:
        start = date.fromisoformat(start_value)
        end = start + timedelta(days=MAX_DATE_RANGE_DAYS)
    elif end_value and not start_value:
        end = date.fromisoformat(end_value)
        start = end - timedelta(days=MAX_DATE_RANGE_DAYS)
    else:
        start = date.fromisoformat(start_value)
        end = date.fromisoformat(end_value)
    if end < start:
        raise ValueError("endDate cannot be earlier than startDate")
    if (end - start).days > MAX_DATE_RANGE_DAYS:
        raise ValueError(f"Date range cannot exceed {MAX_DATE_RANGE_DAYS} days")

    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("SPORTMONKS_API_KEY is not configured")

    league_id = filters.get("leagueId")
    page = max(1, int(filters.get("page") or 1))
    limit = min(max(1, int(filters.get("limit") or MAX_FIXTURE_RESULTS)), MAX_FIXTURE_RESULTS)
    params: dict[str, Any] = {
        "api_token": token,
        "include": "participants;league.country;state;scores",
        "order": "asc",
        "per_page": MAX_FIXTURE_RESULTS,
        "page": page,
    }
    upstream_filters: list[str] = []
    if league_id:
        upstream_filters.append(f"fixtureLeagues:{int(league_id)}")
    participant = _clean(filters.get("homeTeam")) or _clean(filters.get("awayTeam"))
    if participant:
        upstream_filters.append(f"participantSearch:{participant}")
    if upstream_filters:
        params["filters"] = ";".join(upstream_filters)

    print(
        "[match_analysis_search] "
        f"event=request start={start} end={end} country={_clean(filters.get('country'))!r} "
        f"league={_clean(filters.get('league'))!r} league_id={league_id!r} "
        f"home={_clean(filters.get('homeTeam'))!r} away={_clean(filters.get('awayTeam'))!r} "
        f"upstream_filters={params.get('filters', '')!r}",
        flush=True,
    )

    collected: list[dict[str, Any]] = []
    has_more = False
    pages_read = 0
    while pages_read < MAX_UPSTREAM_PAGES and len(collected) < limit:
        response = requests.get(
            f"{SPORTMONKS_BASE_URL}/fixtures/between/{start.isoformat()}/{end.isoformat()}",
            params=params,
            timeout=45,
        )
        if response.status_code != 200:
            detail = "SportMonks fixture request failed"
            try:
                detail = response.json().get("message") or detail
            except ValueError:
                pass
            raise SportMonksError(f"{detail} (HTTP {response.status_code})")
        payload = response.json()
        upstream_rows = payload.get("data") or []
        for raw in upstream_rows:
            item = _fixture_out(raw)
            if not item:
                continue
            if not _matches(item["country"]["name"], filters.get("country")):
                continue
            if not _matches(item["league"]["name"], filters.get("league")):
                continue
            if not _team_matches(item["homeTeam"]["name"], filters.get("homeTeam")):
                continue
            if not _team_matches(item["awayTeam"]["name"], filters.get("awayTeam")):
                continue
            collected.append(item)
            if len(collected) >= limit:
                break
        pagination = payload.get("pagination") or payload.get("meta", {}).get("pagination", {})
        has_more = bool(pagination.get("has_more"))
        pages_read += 1
        if not has_more or len(collected) >= limit:
            break
        params["page"] = int(params["page"]) + 1

    print(
        "[match_analysis_search] "
        f"event=complete upstream_rows={len(upstream_rows)} matched_rows={len(collected)} "
        f"pages_read={pages_read} has_more={has_more}",
        flush=True,
    )
    return {"fixtures": collected, "pagination": {"page": page, "hasMore": has_more, "pagesRead": pages_read}}
