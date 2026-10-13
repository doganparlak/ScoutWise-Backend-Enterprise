"""Authenticated NEC squad browsing, separate from provider player data."""

import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from api_module.database import get_db
from api_module.utilities import require_auth

router = APIRouter(prefix="/nec", dependencies=[Depends(require_auth)])
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def storage_client():
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


def player_payload(row, today):
    player = dict(row)
    dob = player.get("date_of_birth")
    player["age"] = (
        today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if dob else player.get("reported_age")
    )
    player["roles"] = list(dict.fromkeys(player.get("roles") or []))
    bucket, path = player.get("image_storage_bucket"), player.get("image_storage_path")
    if not player.get("image_url") and bucket and path:
        try:
            signed = storage_client().storage.from_(bucket).create_signed_url(path, 3600)
            player["image_url"] = signed.get("signedURL") or signed.get("signedUrl")
        except Exception:
            logger.warning("Could not sign NEC image for player %s", player["id"])
    return player


@router.get("/teams")
def teams(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT t.id, t.name, t.league_name, t.country,
               COALESCE(i.image_url, t.logo_url) AS logo_url,
               i.image_storage_bucket, i.image_storage_path,
               count(p.id) AS player_count
        FROM public.nec_teams t
        LEFT JOIN public.nec_team_images i ON i.team_id = t.id
        LEFT JOIN public.nec_players p ON p.team_id = t.id
        GROUP BY t.id, i.team_id ORDER BY lower(t.name)
    """)).mappings()
    result = []
    for row in rows:
        team = dict(row)
        bucket = team.pop("image_storage_bucket")
        path = team.pop("image_storage_path")
        if not team["logo_url"] and bucket and path:
            try:
                signed = storage_client().storage.from_(bucket).create_signed_url(path, 3600)
                team["logo_url"] = signed.get("signedURL") or signed.get("signedUrl")
            except Exception:
                logger.warning("Could not sign NEC logo for team %s", team["id"])
        result.append(team)
    return result


@router.get("/fixtures")
def fixtures(db: Session = Depends(get_db)):
    team_lookup = {str(team["id"]): team for team in teams(db)}
    roster = db.execute(text("SELECT id, team_id, full_name FROM public.nec_players")).mappings().all()
    rows = db.execute(text("""
        SELECT id, match_date, home_team_id, away_team_id, status,
               home_score, away_score, halftime_home_score, halftime_away_score,
               video_url, events
        FROM public.nec_fixtures ORDER BY match_date DESC, created_at, id
    """)).mappings()
    result = []
    for row in rows:
        fixture = dict(row)
        fixture["home_team"] = team_lookup[str(row["home_team_id"])]
        fixture["away_team"] = team_lookup[str(row["away_team_id"])]
        fixture["events"] = [dict(event) for event in fixture["events"]]
        for event in fixture["events"]:
            team_id = fixture[f"{event['side']}_team_id"]
            for field in ("player", "related_player"):
                event[field + "_id"] = resolve_event_player(event.get(field), team_id, roster)
        result.append(fixture)
    return result


def resolve_event_player(name, team_id, roster):
    """Resolve initials within the event's team, only when the match is unique."""
    if not name:
        return None
    normalize = lambda value: re.findall(r"[a-z]+", value.lower())
    aliases = {
        "a isreal": "Akindipe Israel Bamise",
        "k hassan": "Hassan Kabir Hassan",
        "m ibrahim": "Ibrahim Muhammad",
    }
    tokens = normalize(name)
    canonical = aliases.get(" ".join(tokens))
    candidates = []
    for player in roster:
        if str(player["team_id"]) != str(team_id):
            continue
        full = normalize(player["full_name"])
        matches = (full == normalize(canonical)) if canonical else (
            full == tokens or (len(tokens) >= 2 and len(tokens[0]) == 1
                               and full[0].startswith(tokens[0])
                               and full[1:len(tokens)] == tokens[1:])
        )
        if matches:
            candidates.append(str(player["id"]))
    return candidates[0] if len(candidates) == 1 else None


def build_standings(team_rows, match_rows, roster):
    reference_order = {name: index for index, name in enumerate([
        "Best Talent FC", "Aremu FC", "Elegant Football Academy", "Super Accurate",
        "Ikorodu City Academy", "Rising Root FA", "Lasegski FC", "Saki Star Sport Club",
    ])}
    tables = {}
    for scope in ("all", "home", "away"):
        stats = {str(t["id"]): {"team": t, "played": 0, "won": 0, "drawn": 0,
                 "lost": 0, "goals_for": 0, "goals_against": 0,
                 "goal_difference": 0, "points": 0, "form": []} for t in team_rows}
        for match in sorted(match_rows, key=lambda m: (m["match_date"], str(m["id"]))):
            for side, other in (("home", "away"), ("away", "home")):
                if scope != "all" and scope != side:
                    continue
                row = stats[str(match[side + "_team_id"])]
                scored, conceded = match[side + "_score"], match[other + "_score"]
                row["played"] += 1
                row["goals_for"] += scored
                row["goals_against"] += conceded
                outcome = "W" if scored > conceded else "D" if scored == conceded else "L"
                row[{"W": "won", "D": "drawn", "L": "lost"}[outcome]] += 1
                row["points"] += {"W": 3, "D": 1, "L": 0}[outcome]
                row["form"] = (row["form"] + [outcome])[-5:]
        for row in stats.values():
            row["goal_difference"] = row["goals_for"] - row["goals_against"]
        ordered = sorted(stats.values(), key=lambda r: (
            -r["points"], -r["goal_difference"], -r["goals_for"],
            reference_order.get(r["team"]["name"], 999), r["team"]["name"],
        ))
        tables[scope] = [{**row, "position": index + 1} for index, row in enumerate(ordered)]
    by_id = {str(p["id"]): p for p in roster}
    leaders = {"goals": {}, "assists": {}}
    for match in match_rows:
        for event in match["events"]:
            if event["type"] != "goal":
                continue
            team_id = str(match[event["side"] + "_team_id"])
            for category, field in (("goals", "player"), ("assists", "related_player")):
                name = event.get(field)
                if not name:
                    continue
                player_id = resolve_event_player(name, team_id, roster)
                key = (team_id, player_id or name.lower())
                if key not in leaders[category]:
                    leaders[category][key] = {"player_id": player_id, "team_id": team_id,
                        "name": by_id[player_id]["full_name"] if player_id else name, "value": 0}
                leaders[category][key]["value"] += 1
    for category in leaders:
        ordered = sorted(leaders[category].values(), key=lambda r: (-r["value"], r["name"]))
        position, last_value = 0, None
        for index, row in enumerate(ordered):
            if row["value"] != last_value:
                position = index + 1
            row["position"] = position
            last_value = row["value"]
        leaders[category] = ordered
    return {"tables": tables, **leaders, "matches_played": len(match_rows),
            "total_goals": sum(m["home_score"] + m["away_score"] for m in match_rows)}


@router.get("/standings")
def standings(season: str = Query("2026/27", pattern=r"^20\d{2}/\d{2}$"), db: Session = Depends(get_db)):
    start_year = int(season[:4])
    if int(season[-2:]) != (start_year + 1) % 100:
        raise HTTPException(422, "Invalid season")
    team_rows = teams(db)
    match_rows = db.execute(text("""
        SELECT id, match_date, home_team_id, away_team_id, home_score, away_score, events
        FROM public.nec_fixtures WHERE status = 'finished'
        AND match_date >= make_date(:year, 7, 1)
        AND match_date < make_date(:year + 1, 7, 1)
    """), {"year": start_year}).mappings().all()
    roster = db.execute(text("""
        SELECT id, team_id, full_name, image_url, image_storage_bucket, image_storage_path
        FROM public.nec_players
    """)).mappings().all()
    data = build_standings(team_rows, match_rows, roster)
    by_id = {str(p["id"]): p for p in roster}
    images = {}
    for category in ("goals", "assists"):
        for row in data[category]:
            player_id = row["player_id"]
            if player_id and player_id not in images:
                images[player_id] = player_payload(by_id[player_id], datetime.now().date()).get("image_url")
            row["image_url"] = images.get(player_id)
    return {"season": season, **data}


@router.get("/players/{player_id}")
def player(player_id: UUID, db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT id, team_id, full_name, shirt_number, date_of_birth, birth_year,
               reported_age, age_as_of, roles, position_label, height_cm,
               weight_kg, preferred_foot, nationality, gender, image_url,
               image_storage_bucket, image_storage_path
        FROM public.nec_players WHERE id = :id
    """), {"id": player_id}).mappings().first()
    if not row:
        raise HTTPException(404, "NEC player not found")
    return player_payload(row, datetime.now(ZoneInfo("Europe/Istanbul")).date())


@router.get("/teams/{team_id}/players")
def players(team_id: UUID, db: Session = Depends(get_db)):
    if not db.execute(text("SELECT id FROM public.nec_teams WHERE id = :id"), {"id": team_id}).first():
        raise HTTPException(404, "NEC team not found")
    rows = db.execute(text("""
        SELECT id, team_id, full_name, shirt_number, date_of_birth, birth_year,
               reported_age, age_as_of, roles, position_label, height_cm,
               weight_kg, preferred_foot, nationality, gender, image_url,
               image_storage_bucket, image_storage_path
        FROM public.nec_players WHERE team_id = :id
        ORDER BY shirt_number NULLS LAST, lower(full_name), id
    """), {"id": team_id}).mappings()
    today = datetime.now(ZoneInfo("Europe/Istanbul")).date()
    return [player_payload(row, today) for row in rows]
