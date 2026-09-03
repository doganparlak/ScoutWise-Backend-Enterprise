"""ScoutWise Enterprise pre-match analysis module.

This module is intentionally separate from the completed-match report pipeline.
It will own the data collection, aggregation and report construction required for
Maç Önü Analizi as the report sections are introduced.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Any

import requests

from .match_analysis import SPORTMONKS_BASE_URL, SportMonksError
from match_report_module.report import DERIVED_PERCENTAGE_METRICS, PLAYER_METRIC_CATEGORY, TEAM_METRIC_CATEGORY, _categorize, _derive_team_percentages, _metric, _value


_COMPLETED_STATES = {"FT", "AET", "PEN", "WO", "AP"}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_completed(fixture: dict[str, Any]) -> bool:
    state = fixture.get("state") or {}
    return str(state.get("short_name") or state.get("state") or "").upper() in _COMPLETED_STATES


def _team_location(fixture: dict[str, Any], team_id: int) -> str:
    participant = next(
        (item for item in fixture.get("participants") or [] if _as_int(item.get("id")) == team_id),
        {},
    )
    return str((participant.get("meta") or {}).get("location") or "")


def _lineup_starter(row: dict[str, Any], team_id: int, order: int) -> bool:
    # A starting player is explicitly marked by the formation field or the
    # SportMonks starting-lineup type (11). Do not infer it from the row order:
    # the API can return bench players between starters.
    if row.get("formation_field"):
        return True
    if _as_int(row.get("type_id")) == 11:
        return True
    lineup_type = str((row.get("type") or {}).get("name") or row.get("type_name") or "").casefold()
    return "start" in lineup_type


def _recent_team_fixtures(
    team_id: int,
    season_id: int,
    season_name: str,
    competition_id: int,
    reference_date: str,
) -> list[dict[str, Any]]:
    """Return the five latest completed matches from the active season."""
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")
    try:
        reference = datetime.fromisoformat(reference_date.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        reference = datetime.now(timezone.utc)
    start = (reference.date() - timedelta(days=430)).isoformat()
    end = reference.date().isoformat()
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/fixtures/between/{start}/{end}/{team_id}",
        params={
            "api_token": token,
            "include": "participants;season;state;scores;league.country",
            "per_page": 100,
            "order": "desc",
        },
        timeout=35,
    )
    if response.status_code != 200:
        raise SportMonksError(f"ScoutWise recent fixtures request failed (HTTP {response.status_code})")
    completed_fixtures = [item for item in response.json().get("data") or [] if _is_completed(item)]
    season_fixtures = [
        item
        for item in completed_fixtures
        if not season_id or _as_int((item.get("season") or {}).get("id")) == season_id
    ]
    # Competition-specific season IDs differ from domestic-league season IDs.
    # Match on the season label as the cross-competition fallback (e.g. 2026/27),
    # so a side with no Champions League match can still use its current league form.
    normalized_season_name = re.sub(r"\s+", "", str(season_name or "").casefold())
    same_named_season_fixtures = [
        item
        for item in completed_fixtures
        if normalized_season_name
        and re.sub(r"\s+", "", str((item.get("season") or {}).get("name") or "").casefold())
        == normalized_season_name
    ]
    # Form represents the current season across all competitions. Competition
    # schedules can expose different season IDs for the same season label, so
    # use the union rather than treating the name match as a fallback. This
    # keeps domestic-league and European matches in one latest-five sequence.
    source_by_id = {
        _as_int(item.get("id")): item
        for item in [*season_fixtures, *same_named_season_fixtures]
        if _as_int(item.get("id"))
    }
    source = list(source_by_id.values())
    return sorted(source, key=lambda item: item.get("starting_at") or "", reverse=True)[:5]


def _fixture_lineup_usage(fixture_id: int, team_id: int) -> dict[str, Any]:
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/fixtures/{fixture_id}",
        params={
            "api_token": token,
            "include": "participants;formations;lineups.player;lineups.position;lineups.details.type;events.type;pressure;ballCoordinates;statistics.type;xGFixture.type",
        },
        timeout=35,
    )
    if response.status_code != 200:
        raise SportMonksError(f"ScoutWise historical lineup request failed (HTTP {response.status_code})")
    return response.json().get("data") or {}


def _current_team_player_ids(team_id: int) -> set[int]:
    """Return current senior-squad player IDs from SportMonks' extended squad.

    The extended-squad response is a list of player records: its player ID is
    exposed directly as ``id`` (not ``player_id``). Historical lineup rows use
    that same SportMonks player ID in ``player_id``.
    """
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token or not team_id:
        return set()
    try:
        response = requests.get(
            f"{SPORTMONKS_BASE_URL}/squads/teams/{team_id}/extended",
            params={"api_token": token, "per_page": 250},
            timeout=35,
        )
        if response.status_code != 200:
            print(f"[pre_match_report] event=current_squad_unavailable team_id={team_id} http={response.status_code}")
            return set()
        return {
            _as_int(row.get("id"))
            for row in response.json().get("data") or []
            if row.get("in_squad") is True and _as_int(row.get("id"))
        }
    except requests.RequestException as exc:
        print(f"[pre_match_report] event=current_squad_unavailable team_id={team_id} error={exc}")
        return set()


def _metric_value(row: dict[str, Any], metric_name: str) -> float | None:
    """Read one SportMonks lineup-detail metric without relying on its type id."""
    for detail in row.get("details") or []:
        name = str((detail.get("type") or {}).get("name") or detail.get("name") or "").casefold()
        if name != metric_name.casefold():
            continue
        value = detail.get("data")
        value = value.get("value") if isinstance(value, dict) else value
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


_PLAYER_INSIGHT_EXCLUDED_METRICS = {
    "minutes played", "rating", "appearances", "lineups", "substitutions",
    "captain", "saves", "goals conceded", "penalty saves",
}
_PLAYER_NEGATIVE_METRIC_TERMS = (
    "foul", "card", "offside", "lost", "error", "missed", "conceded",
    "penalty committed", "dribbled past", "passed", "dispossessed", "turn over",
)
_PLAYER_HIGHLIGHT_EXCLUDED_METRICS = {
    "Long Balls Won Percentage",
    "Tacles Won Percentage",
    "Tackles Won Percentage",
    "Tackles Won (%)",
}
# A player drawing a foul is a positive attacking/ball-retention action.  It
# must never be presented as a development risk merely because its provider
# name contains the word "foul".
_PLAYER_DEVELOPMENT_EXCLUDED_METRICS = {
    "fouls drawn",
}
_PLAYER_RATE_METRIC_TERMS = ("percentage", "accuracy", "success rate", "conversion rate")
_DERIVED_PLAYER_PERCENTAGES: dict[str, tuple[str, str]] = {
    "Accurate Passes Percentage": ("Accurate Passes", "Passes"),
    "Long Balls Won Percentage": ("Long Balls Won", "Long Balls"),
    "Aerials Won Percentage": ("Aerials Won", "Aerials"),
    "Duels Won Percentage": ("Duels Won", "Total Duels"),
    "Tackles Won (%)": ("Tackles Won", "Tackles"),
}

# SportMonks has emitted both spellings in lineup details. Canonicalising
# them prevents duplicate metrics and routes the value through i18n.
_PLAYER_METRIC_NAME_ALIASES = {
    "Tacles Won Percentage": "Tackles Won (%)",
    "Tackles Won Percentage": "Tackles Won (%)",
}


def _lineup_numeric_metrics(row: dict[str, Any]) -> dict[str, float]:
    """Return raw numeric lineup metrics; conversion to per-90 happens later."""
    values: dict[str, float] = {}
    for detail in row.get("details") or []:
        name = str((detail.get("type") or {}).get("name") or detail.get("name") or "").strip()
        name = _PLAYER_METRIC_NAME_ALIASES.get(name, name)
        raw = detail.get("data")
        raw = raw.get("value") if isinstance(raw, dict) else raw
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # The report's player-metric mapping is the source of truth for
        # eligible player statistics. This deliberately excludes provider
        # helper fields and all rate values: counts can be correctly summed
        # then normalised per 90, while percentages cannot.
        if (
            name
            and name in PLAYER_METRIC_CATEGORY
            and name.casefold() not in _PLAYER_INSIGHT_EXCLUDED_METRICS
            and "%" not in name
            and not any(term in name.casefold() for term in _PLAYER_RATE_METRIC_TERMS)
        ):
            values[name] = values.get(name, 0.0) + value
    return values


def _lineup_rate_metrics(row: dict[str, Any]) -> dict[str, float]:
    """Return rate metrics separately so they can be minute-weighted.

    A rate such as Duels Won Percentage must never be added across matches or
    run through the per-90 formula. It is a percentage for that appearance;
    the aggregate is the minutes-weighted average of the available values.
    """
    values: dict[str, float] = {}
    for detail in row.get("details") or []:
        name = str((detail.get("type") or {}).get("name") or detail.get("name") or "").strip()
        name = _PLAYER_METRIC_NAME_ALIASES.get(name, name)
        raw = detail.get("data")
        raw = raw.get("value") if isinstance(raw, dict) else raw
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if (
            name
            and name in PLAYER_METRIC_CATEGORY
            and ("%" in name or any(term in name.casefold() for term in _PLAYER_RATE_METRIC_TERMS))
        ):
            values[name] = value
    return values


def _attach_player_highlight_metrics(players: list[dict[str, Any]]) -> None:
    """Pick three role-output metrics from each eligible player's per-90 sample.

    Positive selections are the player's strongest relative outputs in the
    selected team sample. Development selections deliberately use the lowest
    relative productive outputs, never the player's strongest numbers.
    """
    metric_population: dict[str, list[float]] = defaultdict(list)
    for player in players:
        for metric in player.get("per90_metrics") or []:
            metric_population[str(metric["name"])].append(float(metric["value"]))

    for player in players:
        scored = []
        risk_scored = []
        for metric in player.get("per90_metrics") or []:
            if str(metric.get("name") or "") in _PLAYER_HIGHLIGHT_EXCLUDED_METRICS:
                continue
            values = metric_population.get(str(metric["name"]), [])
            if not values:
                continue
            value = float(metric["value"])
            percentile = sum(sample <= value for sample in values) / len(values)
            entry = {**metric, "relative_score": percentile}
            name = str(metric["name"]).casefold()
            if name in _PLAYER_DEVELOPMENT_EXCLUDED_METRICS:
                scored.append(entry)
                continue
            category = (PLAYER_METRIC_CATEGORY.get(str(metric["name"])) or ("", ""))[0]
            if category == "errors_discipline" or any(term in name for term in _PLAYER_NEGATIVE_METRIC_TERMS):
                # For risk metrics, a higher per-90 output is the area that
                # requires attention. It must not be promoted as a strength.
                risk_scored.append(entry)
            else:
                scored.append(entry)
        player["standout_metrics"] = sorted(
            scored, key=lambda item: (-item["relative_score"], -item["value"], item["name"])
        )[:3]
        # Lead with errors/discipline where that player's selected-match
        # record provides it. When none is recorded, show the lowest
        # productive outputs rather than inventing a risk.
        player["development_metrics"] = (
            sorted(risk_scored, key=lambda item: (-item["relative_score"], -item["value"], item["name"]))[:3]
            or sorted(scored, key=lambda item: (item["relative_score"], item["value"], item["name"]))[:3]
        )


def _current_score(fixture: dict[str, Any], location: str) -> int | None:
    score = next(
        (
            item
            for item in fixture.get("scores") or []
            if item.get("description") == "CURRENT"
            and (item.get("score") or {}).get("participant") == location
        ),
        {},
    )
    value = (score.get("score") or {}).get("goals")
    return int(value) if isinstance(value, (int, float)) else None


def _aggregate_team_comparison(
    fixtures: list[dict[str, Any]],
    team_id: int,
) -> dict[str, list[dict[str, Any]]]:
    """Aggregate only metrics available in a team's selected fixtures.

    Count metrics are summed and then shown per available match. Known rate
    metrics are recalculated from their aggregate numerator and denominator;
    other provider rates are averaged. Keeping matches_covered per metric makes
    the comparison robust when the provider omits a metric in part of a sample.
    """
    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for fixture in fixtures:
        statistic_rows = [
            row for row in fixture.get("statistics") or []
            if _as_int(row.get("participant_id")) == team_id
        ]
        categories, _ = _categorize(statistic_rows, TEAM_METRIC_CATEGORY)
        _derive_team_percentages(categories)
        expected = [
            _metric(
                str((row.get("type") or {}).get("name") or row.get("type_id") or ""),
                _value(row),
                row.get("type_id"),
            )
            for row in fixture.get("xgfixture") or []
            if _as_int(row.get("participant_id")) == team_id
            and str((row.get("type") or {}).get("name") or "") != "Expected Goals Against (xGA)"
        ]
        for group, metrics in {**categories, "expected": expected}.items():
            for metric in metrics:
                try:
                    value = float(metric.get("value"))
                except (TypeError, ValueError):
                    continue
                name = str(metric.get("name") or "").strip()
                if name:
                    samples[group][name].append(value)
    aggregate: dict[str, list[dict[str, Any]]] = {}
    for group, metrics in samples.items():
        rows = []
        for name, values in metrics.items():
            total = sum(values)
            average = total / len(values)
            rate = "%" in name or "percentage" in name.casefold() or "performance" in name.casefold()
            numerator_denominator = DERIVED_PERCENTAGE_METRICS.get(name)
            numerator = sum(metrics.get(numerator_denominator[0], [])) if numerator_denominator else 0.0
            denominator = sum(metrics.get(numerator_denominator[1], [])) if numerator_denominator else 0.0
            derived_rate = numerator / denominator * 100 if numerator_denominator and denominator > 0 else None
            rows.append({
                "name": name,
                # Per-match is the comparison default for multi-match samples.
                "value": round(derived_rate, 2) if derived_rate is not None else round(average, 2),
                "total": round(total, 2),
                "aggregation": "derived_rate" if derived_rate is not None else "average" if rate else "per_match",
                "matches_covered": len(values),
            })
        if rows:
            aggregate[group] = sorted(rows, key=lambda row: row["name"])
    return aggregate


def _aggregate_score_flow(fixtures: list[dict[str, Any]], team_id: int) -> dict[str, Any]:
    """Build the same ahead/level/behind state model used in team analysis."""
    state_order = ("ahead", "level", "behind")
    totals = {
        key: {"minutes": 0.0, "segments": 0, "matches_entered": 0, "goals_for": 0, "goals_against": 0, "to_ahead": 0, "to_level": 0, "to_behind": 0, "final_wins": 0, "final_draws": 0, "final_losses": 0}
        for key in state_order
    }

    def game_state(team_score: int, opponent_score: int) -> str:
        return "ahead" if team_score > opponent_score else "behind" if team_score < opponent_score else "level"

    for fixture in fixtures:
        location = _team_location(fixture, team_id)
        home = location == "home"
        try:
            duration = max(90.0, float(fixture.get("length") or 90))
        except (TypeError, ValueError):
            duration = 90.0
        goals = []
        for event in fixture.get("events") or []:
            event_type = str((event.get("type") or {}).get("name") or event.get("type_name") or "").casefold()
            result = str(event.get("result") or "")
            has_score = bool(re.fullmatch(r"\s*\d+\s*[-:]\s*\d+\s*", result))
            if ("goal" not in event_type and not has_score) or any(word in event_type for word in ("disallow", "cancel", "miss")):
                continue
            try:
                minute = float(event.get("minute") or 0) + float(event.get("extra_minute") or 0)
            except (TypeError, ValueError):
                continue
            if minute < 0:
                continue
            goals.append({"minute": minute, "team_id": _as_int(event.get("participant_id") or event.get("team_id")), "result": result})
            duration = max(duration, minute)
        goals.sort(key=lambda item: item["minute"])
        team_score = opponent_score = 0
        cursor = 0.0
        current = "level"
        entered = {current}
        totals[current]["segments"] += 1
        for goal in goals:
            minute = min(duration, max(cursor, goal["minute"]))
            totals[current]["minutes"] += minute - cursor
            parsed = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", goal["result"])
            if parsed:
                home_score, away_score = int(parsed.group(1)), int(parsed.group(2))
                next_team, next_opponent = (home_score, away_score) if home else (away_score, home_score)
                scoring_for = next_team > team_score
                team_score, opponent_score = next_team, next_opponent
            else:
                scoring_for = goal["team_id"] == team_id
                team_score += int(scoring_for)
                opponent_score += int(not scoring_for)
            totals[current]["goals_for" if scoring_for else "goals_against"] += 1
            next_state = game_state(team_score, opponent_score)
            if next_state != current:
                totals[current][f"to_{next_state}"] += 1
                totals[next_state]["segments"] += 1
                entered.add(next_state)
                current = next_state
            cursor = minute
        totals[current]["minutes"] += max(0.0, duration - cursor)
        outcome = "final_wins" if team_score > opponent_score else "final_losses" if team_score < opponent_score else "final_draws"
        for key in entered:
            totals[key]["matches_entered"] += 1
            totals[key][outcome] += 1

    total_minutes = sum(totals[key]["minutes"] for key in state_order) or 1.0
    return {
        "states": [
            {
                "key": key,
                "minutes": round(totals[key]["minutes"], 1),
                "share": round(totals[key]["minutes"] / total_minutes * 100, 1),
                "average_spell": round(totals[key]["minutes"] / totals[key]["segments"], 1) if totals[key]["segments"] else 0,
                **{field: int(value) for field, value in totals[key].items() if field not in {"minutes", "segments"}},
            }
            for key in state_order
        ]
    }


def _team_recent_squad_usage(
    team: dict[str, Any],
    season_id: int,
    season_name: str,
    competition_id: int,
    reference_date: str,
) -> dict[str, Any]:
    team_id = _as_int(team.get("id"))
    fallback = {
        "team_id": team_id,
        "sample_size": 0,
        "current_squad_count": 0,
        "players": [],
        "formations": [],
    }
    if not team_id:
        return fallback
    current_player_ids = _current_team_player_ids(team_id)
    fixtures = _recent_team_fixtures(team_id, season_id, season_name, competition_id, reference_date)
    if not fixtures:
        return fallback
    fixture_ids = [_as_int(item.get("id")) for item in fixtures if _as_int(item.get("id"))]
    with ThreadPoolExecutor(max_workers=min(5, len(fixture_ids))) as executor:
        full_fixtures = list(executor.map(lambda fixture_id: _fixture_lineup_usage(fixture_id, team_id), fixture_ids))

    players: dict[int, dict[str, Any]] = {}
    starts: Counter[int] = Counter()
    substitute_appearances: Counter[int] = Counter()
    formation_counts: Counter[str] = Counter()
    player_minutes: Counter[int] = Counter()
    player_goals: Counter[int] = Counter()
    player_rating_weighted: Counter[int] = Counter()
    player_rating_minutes: Counter[int] = Counter()
    player_metric_totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    player_metric_minutes: dict[int, Counter[str]] = defaultdict(Counter)
    player_rate_weighted_totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    player_rate_minutes: dict[int, Counter[str]] = defaultdict(Counter)
    player_derived_rate_numerators: dict[int, Counter[str]] = defaultdict(Counter)
    player_derived_rate_denominators: dict[int, Counter[str]] = defaultdict(Counter)
    pressure_samples: dict[int, list[float]] = {}
    momentum_events: list[dict[str, Any]] = []
    territory_counts = [0] * 9
    for fixture_index, fixture in enumerate(full_fixtures):
        # Formation is recorded per participant. A fixture without formation
        # data still contributes to the player sample, not its formation rate.
        formation = next(
            (
                item
                for item in fixture.get("formations") or []
                if _as_int(item.get("participant_id") or item.get("team_id")) == team_id
            ),
            {},
        )
        formation_name = str(formation.get("formation") or formation.get("name") or "").strip()
        if formation_name:
            formation_counts[formation_name] += 1

        team_rows = [
            row
            for row in fixture.get("lineups") or []
            if _as_int(row.get("team_id") or row.get("participant_id")) == team_id
        ]
        for index, row in enumerate(team_rows):
            player = row.get("player") or {}
            position = row.get("position") or {}
            player_id = _as_int(row.get("player_id") or player.get("id"))
            if not player_id:
                continue
            players[player_id] = {
                **players.get(player_id, {}),
                "player_id": player_id,
                "player_name": row.get("player_name") or player.get("display_name") or player.get("name") or "—",
                "player_image_url": player.get("image_path") or row.get("image_path"),
                "position_name": position.get("name") or row.get("position_name") or "",
                "position_id": row.get("position_id") or position.get("id"),
                "last_match_starter": bool(players.get(player_id, {}).get("last_match_starter")),
            }
            if _lineup_starter(row, team_id, index):
                starts[player_id] += 1
                if fixture_index == 0:
                    players[player_id]["last_match_starter"] = True

            minutes = _metric_value(row, "Minutes Played") or 0.0
            rating = _metric_value(row, "Rating")
            player_minutes[player_id] += minutes
            if rating is not None and minutes > 0:
                player_rating_weighted[player_id] += rating * minutes
                player_rating_minutes[player_id] += minutes
            if minutes > 0:
                count_metrics = _lineup_numeric_metrics(row)
                for metric_name, metric_value in count_metrics.items():
                    player_metric_totals[player_id][metric_name] += metric_value
                    # Missing provider details are unavailable, not silently
                    # treated as a zero. Each metric therefore receives the
                    # minutes from appearances where it is actually present.
                    player_metric_minutes[player_id][metric_name] += minutes
                for rate_name, (numerator_name, denominator_name) in _DERIVED_PLAYER_PERCENTAGES.items():
                    if numerator_name in count_metrics and denominator_name in count_metrics:
                        player_derived_rate_numerators[player_id][rate_name] += count_metrics[numerator_name]
                        player_derived_rate_denominators[player_id][rate_name] += count_metrics[denominator_name]
            # Percentages retain their meaning only as a minute-weighted
            # average across appearances with that metric available.
            if minutes > 0:
                for metric_name, metric_value in _lineup_rate_metrics(row).items():
                    if metric_name in _DERIVED_PLAYER_PERCENTAGES:
                        continue
                    player_rate_weighted_totals[player_id][metric_name] += metric_value * minutes
                    player_rate_minutes[player_id][metric_name] += minutes

        # In SportMonks substitution events, player_id is the player entering
        # the field and related_player_id is the player leaving it.
        for event in fixture.get("events") or []:
            if _as_int(event.get("participant_id") or event.get("team_id")) != team_id:
                continue
            event_type = event.get("type") or {}
            type_name = str(event_type.get("name") or event.get("type_name") or "").casefold()
            if _as_int(event.get("type_id")) != 18 and "substitution" not in type_name:
                continue
            player_id = _as_int(event.get("player_id"))
            if player_id:
                substitute_appearances[player_id] += 1

            # Goals are attributed to the event player, so they remain correct
            # even when a lineup detail is unavailable for that fixture.
        for event in fixture.get("events") or []:
            if _as_int(event.get("participant_id") or event.get("team_id")) != team_id:
                continue
            event_type = event.get("type") or {}
            type_name = str(event_type.get("name") or event.get("type_name") or "").casefold()
            if "goal" not in type_name or "own goal" in type_name:
                continue
            scorer_id = _as_int(event.get("player_id"))
            if scorer_id:
                player_goals[scorer_id] += 1

        own_pressure: Counter[int] = Counter()
        opponent_pressure: Counter[int] = Counter()
        for point in fixture.get("pressure") or []:
            minute = _as_int(point.get("minute"))
            if not minute:
                continue
            try:
                value = float(point.get("pressure") or 0)
            except (TypeError, ValueError):
                continue
            target = own_pressure if _as_int(point.get("participant_id")) == team_id else opponent_pressure
            target[minute] += value
        for minute in set(own_pressure) | set(opponent_pressure):
            pressure_samples.setdefault(minute, []).append(own_pressure[minute] - opponent_pressure[minute])

        # Goals scored by the opponent are kept separately for the same
        # 10-minute intervals. Own goals are intentionally excluded from the
        # scorer loop above, but still count correctly here by event team.
        for event in fixture.get("events") or []:
            event_type = event.get("type") or {}
            type_name = str(event_type.get("name") or event.get("type_name") or "").casefold()
            if "goal" not in type_name:
                continue
            minute = _as_int(event.get("minute"))
            if not minute:
                continue
            event_team_id = _as_int(event.get("participant_id") or event.get("team_id"))
            if "own goal" in type_name:
                is_for = event_team_id != team_id
            else:
                is_for = event_team_id == team_id
            momentum_events.append({
                "minute": minute,
                "goals_for": 1 if is_for else 0,
                "goals_against": 0 if is_for else 1,
            })

        # Ball-coordinate data does not identify a participant. As in the
        # team-analysis report, this is the spatial character of this team's
        # selected match sample, aggregated independently for each side.
        for coordinate in fixture.get("ballcoordinates") or []:
            try:
                x = min(0.999999, max(0.0, float(coordinate.get("x") or 0)))
                y = min(0.999999, max(0.0, float(coordinate.get("y") or 0)))
            except (TypeError, ValueError):
                continue
            territory_counts[int(y * 3) * 3 + int(x * 3)] += 1

    # Player-specific sections must reflect the club's current squad, rather
    # than historical appearances by players who have since transferred.
    current_players = {
        player_id: player
        for player_id, player in players.items()
        if player_id in current_player_ids
    }
    rows = []
    for player_id, player in current_players.items():
        starter_count = int(starts[player_id])
        substitute_count = int(substitute_appearances[player_id])
        if starter_count or substitute_count:
            minutes = float(player_minutes[player_id])
            rating_minutes = float(player_rating_minutes[player_id])
            # A player's rating is shown in the squad sheet only after a
            # meaningful 90-minute contribution. It is duration-weighted so
            # a brief substitute appearance cannot distort the average.
            average_rating = (
                round(float(player_rating_weighted[player_id]) / rating_minutes, 2)
                if minutes >= 90 and rating_minutes > 0
                else None
            )
            rows.append({
                **player,
                "starts": starter_count,
                "substitute_appearances": substitute_count,
                "minutes": round(minutes),
                "average_rating": average_rating,
            })
    rows.sort(key=lambda item: (-item["starts"], -item["substitute_appearances"], item["player_name"]))
    eligible_performers = []
    for player_id, player in current_players.items():
        minutes = float(player_minutes[player_id])
        rating_minutes = float(player_rating_minutes[player_id])
        if minutes <= 90 or not rating_minutes:
            continue
        eligible_performers.append({
            **player,
            "minutes": round(minutes),
            "average_rating": round(float(player_rating_weighted[player_id]) / rating_minutes, 2),
            "goals": int(player_goals[player_id]),
            "per90_metrics": [
                {
                    "name": name,
                    "value": round(value * 90 / player_metric_minutes[player_id][name], 2),
                    "is_percentage": False,
                }
                for name, value in player_metric_totals[player_id].items()
                if player_metric_minutes[player_id][name] > 0 and value >= 0
            ] + [
                {
                    "name": name,
                    "value": round(player_derived_rate_numerators[player_id][name] * 100 / denominator, 2),
                    "is_percentage": True,
                }
                for name, denominator in player_derived_rate_denominators[player_id].items()
                if denominator > 0
            ] + [
                {
                    "name": name,
                    "value": round(weighted_value / player_rate_minutes[player_id][name], 2),
                    "is_percentage": True,
                }
                for name, weighted_value in player_rate_weighted_totals[player_id].items()
                if player_rate_minutes[player_id][name] > 0
            ],
        })
    _attach_player_highlight_metrics(eligible_performers)
    rating_order = sorted(
        eligible_performers,
        key=lambda item: (-item["average_rating"], -item["minutes"], item["player_name"]),
    )
    goal_order = sorted(
        eligible_performers,
        key=lambda item: (-item["goals"], -item["average_rating"], -item["minutes"], item["player_name"]),
    )
    top_rated = rating_order[0] if rating_order else None
    top_goal_count = goal_order[0]["goals"] if goal_order and goal_order[0]["goals"] > 0 else 0
    # When the goal lead is shared, keep the two strongest rated scorers from
    # that tie rather than arbitrarily presenting a single player.
    top_scorers = [
        item for item in goal_order if item["goals"] == top_goal_count
    ][:2] if top_goal_count else []
    top_scorer = top_scorers[0] if top_scorers else None
    # The report always presents two distinct standout players. Prefer the
    # top-rated player plus the leading scorer; where that yields one person
    # (or no scorer), complete the pair with the next highest-rated player.
    featured_players: list[dict[str, Any]] = []
    for candidate in [top_rated, *top_scorers, *rating_order]:
        if candidate and all(candidate["player_id"] != item["player_id"] for item in featured_players):
            featured_players.append(candidate)
        if len(featured_players) == 2:
            break
    # Goalkeepers are evaluated through a distinct role profile and should not
    # be surfaced as a development-risk player beside outfield contributors.
    outfield_performers = [
        player for player in eligible_performers
        if str(player.get("position_name") or "").strip().casefold()
        not in {"goalkeeper", "kaleci"}
    ]
    development = sorted(
        outfield_performers,
        key=lambda item: (item["average_rating"], -item["minutes"], item["player_name"]),
    )[0] if outfield_performers else None
    windows = ((0, 10), (11, 20), (21, 30), (31, 40), (41, 50), (51, 60), (61, 70), (71, 80), (81, 90), (91, None))
    momentum_rows = []
    for start, end in windows:
        pressure_values = [
            value
            for minute, values in pressure_samples.items()
            if minute >= start and (end is None or minute <= end)
            for value in values
        ]
        scoped_events = [
            event for event in momentum_events
            if event["minute"] >= start and (end is None or event["minute"] <= end)
        ]
        momentum_rows.append({
            "period": "90+" if end is None else f"{start}-{end}",
            "average_net_pressure": round(sum(pressure_values) / len(pressure_values), 2) if pressure_values else 0,
            "goals_for": sum(event["goals_for"] for event in scoped_events),
            "goals_against": sum(event["goals_against"] for event in scoped_events),
        })
    territory_total = sum(territory_counts) or 1
    sample_size = len(fixtures)
    results: list[dict[str, Any]] = []
    # The request is newest-first and the report follows that order.
    for fixture in fixtures:
        location = _team_location(fixture, team_id)
        opponent = next(
            (item for item in fixture.get("participants") or [] if _as_int(item.get("id")) != team_id),
            {},
        )
        team_score = _current_score(fixture, location)
        opponent_score = _current_score(
            fixture,
            "away" if location == "home" else "home",
        )
        result = ""
        if team_score is not None and opponent_score is not None:
            result = "W" if team_score > opponent_score else "L" if team_score < opponent_score else "D"
        results.append(
            {
                "fixture_id": _as_int(fixture.get("id")),
                "starting_at": fixture.get("starting_at") or "",
                "opponent_name": opponent.get("name") or "—",
                "opponent_image_url": opponent.get("image_path"),
                "location": location,
                "team_score": team_score,
                "opponent_score": opponent_score,
                "result": result,
                "league_name": (fixture.get("league") or {}).get("name") or "",
            }
        )
    wins = sum(item["result"] == "W" for item in results)
    draws = sum(item["result"] == "D" for item in results)
    losses = sum(item["result"] == "L" for item in results)
    def score_summary(location: str | None = None) -> dict[str, int]:
        selected = [item for item in results if location is None or item["location"] == location]
        return {
            "matches": len(selected),
            "goals_for": sum(int(item["team_score"] or 0) for item in selected),
            "goals_against": sum(int(item["opponent_score"] or 0) for item in selected),
        }
    unique_substitutes = sum(1 for player_id in players if substitute_appearances[player_id] > 0)
    unique_starters = sum(1 for player_id in players if starts[player_id] > 0)
    rotation_base = unique_substitutes + unique_starters
    total_rating_minutes = sum(float(value) for value in player_rating_minutes.values())
    team_average_rating = (
        round(sum(float(value) for value in player_rating_weighted.values()) / total_rating_minutes, 2)
        if total_rating_minutes > 0
        else None
    )
    return {
        "team_id": team_id,
        "sample_size": sample_size,
        "current_squad_count": len(current_player_ids),
        "players": rows,
        "formations": [
            {
                "formation": name,
                "matches": count,
                "percentage": round((count / sample_size) * 100, 1),
            }
            for name, count in formation_counts.most_common()
        ],
        "results": results,
        "performance_summary": {
            "top_rated": top_rated,
            "top_scorer": top_scorer,
            "top_scorers": top_scorers,
            "featured_players": featured_players,
            "development": development,
        },
        "momentum": momentum_rows,
        "territory": [
            {
                "index": index,
                "count": count,
                "percentage": round((count / territory_total) * 100, 1),
            }
            for index, count in enumerate(territory_counts)
        ],
        "team_comparison": _aggregate_team_comparison(full_fixtures, team_id),
        "score_flow": _aggregate_score_flow(full_fixtures, team_id),
        "summary": {
            "matches": sample_size,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_for": sum(int(item["team_score"] or 0) for item in results),
            "goals_against": sum(int(item["opponent_score"] or 0) for item in results),
            "home": score_summary("home"),
            "away": score_summary("away"),
            "rotation_level": round((unique_substitutes / rotation_base) * 100, 1) if rotation_base else 0,
            "average_rating": team_average_rating,
        },
    }


def _head_to_head_results(
    home_team: dict[str, Any],
    away_team: dict[str, Any],
    current_season_id: int,
    reference_date: str,
) -> dict[str, list[dict[str, Any]]]:
    """Find completed head-to-head fixtures in the current and previous season."""
    home_id, away_id = _as_int(home_team.get("id")), _as_int(away_team.get("id"))
    if not home_id or not away_id:
        return {"matches": []}
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")
    try:
        reference = datetime.fromisoformat(reference_date.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        reference = datetime.now(timezone.utc)
    params = {
        "api_token": token,
        "include": "participants;season;state;scores;league.country",
        "per_page": 100,
        "order": "desc",
        "page": 1,
    }
    raw_fixtures: list[dict[str, Any]] = []
    while True:
        response = requests.get(
            f"{SPORTMONKS_BASE_URL}/fixtures/between/{(reference.date() - timedelta(days=430)).isoformat()}/{reference.date().isoformat()}/{home_id}",
            params=params,
            timeout=35,
        )
        if response.status_code != 200:
            raise SportMonksError(f"ScoutWise head-to-head request failed (HTTP {response.status_code})")
        payload = response.json()
        raw_fixtures.extend(payload.get("data") or [])
        if not (payload.get("pagination") or {}).get("has_more"):
            break
        params["page"] = int(params["page"]) + 1
    fixtures = [
        item
        for item in raw_fixtures
        if _is_completed(item)
        and any(_as_int(participant.get("id")) == away_id for participant in item.get("participants") or [])
    ]

    def serialize(item: dict[str, Any]) -> dict[str, Any]:
        participants = item.get("participants") or []
        home = next((participant for participant in participants if (participant.get("meta") or {}).get("location") == "home"), {})
        away = next((participant for participant in participants if (participant.get("meta") or {}).get("location") == "away"), {})
        return {
            "fixture_id": _as_int(item.get("id")),
            "starting_at": item.get("starting_at") or "",
            "home_name": home.get("name") or "—",
            "home_image_url": home.get("image_path"),
            "away_name": away.get("name") or "—",
            "away_image_url": away.get("image_path"),
            "home_score": _current_score(item, "home"),
            "away_score": _current_score(item, "away"),
            "league_name": (item.get("league") or {}).get("name") or "",
            "season_name": (item.get("season") or {}).get("name") or "",
        }

    return {"matches": [serialize(item) for item in sorted(fixtures, key=lambda value: value.get("starting_at") or "", reverse=True)]}


def _pre_match_player_perspectives(usages: list[dict[str, Any]], lang: str) -> dict[str, dict[str, str]]:
    selected: list[tuple[dict[str, Any], str]] = []
    for usage in usages:
        summary = usage.get("performance_summary") or {}
        featured = summary.get("featured_players") or [
            summary.get("top_rated"),
            *(summary.get("top_scorers") or []),
        ]
        seen: set[int] = set()
        for player in featured:
            if isinstance(player, dict) and _as_int(player.get("player_id")) not in seen:
                seen.add(_as_int(player.get("player_id")))
                selected.append((player, "featured"))
        if isinstance(summary.get("development"), dict):
            selected.append((summary["development"], "development"))

    fallback: dict[str, dict[str, str]] = {}
    compact = []
    for player, selection in selected:
        key = str(player.get("player_id") or player.get("player_name"))
        metrics = player.get("development_metrics" if selection == "development" else "standout_metrics") or []
        name = str(player.get("player_name") or "Oyuncu")
        fallback[key] = {
            "text": (
                f"{name}, seçili maçlarda rolüne uygun tekrar eden bir katkı sundu; bu profil takımın ilgili oyun alanındaki üretimini destekliyor."
                if selection == "featured" and lang == "tr" else
                f"{name}'ın seçili maçlarda düşük kalan rol katkısı, daha istikrarlı üretim oluşturması gereken alanı öne çıkarıyor."
                if lang == "tr" else
                f"{name}'s output shows a recurring contribution within the role across the selected matches."
                if selection == "featured" else
                f"{name}'s lower role output identifies the area requiring more consistent contribution."
            )
        }
        compact.append({
            "player_id": player.get("player_id"), "player_name": name,
            "selection_type": selection, "position": player.get("position_name"),
            "average_rating": player.get("average_rating"),
            "per90_metrics": metrics,
        })
    if not compact or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior football analyst. Return only valid JSON keyed by player_id. Each value must be one concise 35-55 word ScoutWise perspective in the requested language. Explain why the player was selected; interpret the role and combined effect rather than reciting values. Featured players: use only positive evidence. Development players: use only the supplied weaker per-90 outputs, never praise or select a strength. Never use the Turkish word 'örneklem'. Never invent facts, tactics, benchmarks, causation, or recommendations. No markdown/headings."),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nPlayers: {json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                text = value.get("text") if isinstance(value, dict) else value
                if str(text or "").strip():
                    fallback[str(key)] = {"text": str(text).strip()}
    except Exception as exc:
        print(f"[pre_match_report] event=player_perspective_fallback error={exc}")
    return fallback


def _pre_match_team_analysis(usages: list[dict[str, Any]], teams: list[dict[str, Any]], lang: str) -> dict[str, dict[str, str]]:
    compact = []
    fallback: dict[str, dict[str, str]] = {}
    for usage, team in zip(usages, teams):
        team_id = _as_int(team.get("id"))
        summary = usage.get("summary") or {}
        categories = usage.get("team_comparison") or {}
        compact.append({"team_id": team_id, "team_name": team.get("name"), "fixture_location": team.get("location"), "form": summary, "metrics": categories})
        fallback[str(team_id)] = {
            "positive": "Seçili maçlardaki üretim, takımın tekrar eden güçlü oyun alanlarını gösteriyor." if lang == "tr" else "The selected-match output reveals the team's recurring strengths.",
            "strategy": "Rakibin alan bırakabildiği anlarda bu üretimi daha sık son bölgeye taşımak belirleyici olacaktır." if lang == "tr" else "Turning this output into more final-third actions when space appears against the opponent will be decisive.",
            "weakness": "Maçlardaki dalgalanan değerler, rakip baskısı altında korunması gereken gelişim alanını işaret ediyor." if lang == "tr" else "Variation across the selected matches identifies the area that must be protected under opponent pressure.",
        }
    if len(compact) != 2 or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior pre-match football analyst. Return only valid JSON keyed by team_id. Every team value MUST contain exactly positive, strategy, weakness. Each team's fixture_location specifies whether this upcoming match is home or away. Treat it as mandatory context: do not mention, rely on, or infer from that team's opposite-venue form. For example, an away team must never be described through home strength. positive and weakness must each be a 60-75 word, insight-led analysis; strategy must be an 85-100 word opponent-specific match plan. In positive and weakness, use at most two carefully chosen numbers only when they materially support the conclusion; prioritise tactical meaning, recurring behaviour, matchup implications and risk over metric recitation. strategy is STRICTLY plan and recommendations: do not mention any number, percentage, metric name, stat, or data value. For each team_id, strategy is advice for THAT team and must explicitly refer to the other supplied team by its name as the opponent. In Turkish, never use direct commands or second-person imperatives such as 'kurun', 'sıkıştırın', 'bırakın'; use formal analytical recommendations such as 'daraltılmalı', 'yönlendirilmelidir', 'korunmalıdır', 'uygulanmalı'. State how to defend, progress, press, create advantages and manage likely game states against this specific opponent. Never use the Turkish word 'örneklem'. Never invent unavailable tactics, players, facts, benchmarks or certainty. No markdown, headings, bullets, or leading dashes."),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam evidence: {json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        required_fields = ("positive", "strategy", "weakness")
        needs_expansion = isinstance(parsed, dict) and any(
            not isinstance(value, dict)
            or len(str(value.get("positive") or "").split()) < 60
            or len(str(value.get("weakness") or "").split()) < 60
            or len(str(value.get("strategy") or "").split()) < 85
            or bool(re.search(r"\d", str(value.get("strategy") or "")))
            for value in parsed.values()
        )
        if needs_expansion:
            try:
                revision = ChatOpenAI(
                    model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
                    api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
                ).invoke([
                    ("system", "Rewrite the supplied pre-match analysis as valid JSON only. Preserve the exact team_id / positive / strategy / weakness schema. Respect each team's fixture_location strictly: do not mention or derive conclusions from the opposite venue; an away team must not be described through home form. positive and weakness must each contain 60-75 words; focus on interpretation and use at most two numbers only if essential. strategy must contain 85-100 words and be a pure opponent-specific action plan: it must contain no numbers, percentages, metric names, statistics or data values. Every strategy must advise the team identified by its own team_id and explicitly name the other team as opponent. In Turkish use only formal analytical recommendation language such as 'yapılmalı', 'edilmeli', 'korunmalıdır'—never direct commands or second-person imperatives. Explain pressing, progression, defending, chance creation and game-state management in tactical terms. Do not pad by restating data. Do not use markdown, headings, bullets, leading dashes, invented facts, or the Turkish word 'örneklem'."),
                    ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam evidence: {json.dumps(compact, ensure_ascii=False, default=str)}\nDraft to expand: {json.dumps(parsed, ensure_ascii=False, default=str)}"),
                ])
                revised = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(revision.content or "").strip(), flags=re.I))
                if isinstance(revised, dict):
                    parsed = revised
            except Exception as exc:
                # A formatting failure in the optional rewrite must never discard
                # the valid first-pass analysis and force a generic fallback.
                print(f"[pre_match_report] event=team_analysis_revision_skipped error={exc}")
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if isinstance(value, dict) and all(str(value.get(field) or "").strip() for field in required_fields):
                    fallback[str(key)] = {field: str(value[field]).strip() for field in required_fields}
    except Exception as exc:
        print(f"[pre_match_report] event=team_analysis_fallback error={exc}")
    return fallback


def _pre_match_team_comparison_perspectives(
    usages: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    lang: str,
) -> dict[str, str]:
    """Create one evidence-led comparison interpretation for every category."""
    categories = sorted({
        category
        for usage in usages
        for category, metrics in (usage.get("team_comparison") or {}).items()
        if metrics
    })
    evidence = {
        category: {
            str(team.get("name") or "Team"): (usage.get("team_comparison") or {}).get(category, [])
            for usage, team in zip(usages, teams)
        }
        for category in categories
    }
    fallback = {
        category: (
            "İki takımın bu kategorideki değerleri, maçın ilgili oyun alanında belirginleşebilecek farkı gösteriyor."
            if lang == "tr" else
            "The two teams' values in this category show the difference that may shape this area of the match."
        )
        for category in categories
    }
    if not categories or len(usages) != 2 or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            (
                "system",
                "You are ScoutWise Enterprise's senior pre-match football analyst. Return only valid JSON whose keys exactly match the supplied category keys. Each value must be one 45-65 word ScoutWise perspective in the requested language. Compare the two teams only within that category, identify the most meaningful contrast or shared tendency, then state the likely football implication for the upcoming match. Interpret rather than list values; use at most two short numerical facts. Never use the Turkish word 'örneklem'. Never invent unavailable tactics, players, external benchmarks, certainty, or causation. No markdown, headings, bullets, or leading dashes.",
            ),
            (
                "human",
                f"Language: {'Turkish' if lang == 'tr' else 'English'}\nRequired category keys: {json.dumps(categories, ensure_ascii=False)}\nCategory evidence: {json.dumps(evidence, ensure_ascii=False, default=str)}",
            ),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if isinstance(parsed, dict):
            for category in categories:
                text = str(parsed.get(category) or "").strip()
                if text:
                    fallback[category] = text
    except Exception as exc:
        print(f"[pre_match_report] event=team_comparison_perspective_fallback error={exc}")
    return fallback


def _pre_match_momentum_perspectives(
    usages: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    lang: str,
) -> dict[str, Any]:
    """Interpret each side's pressure windows and their likely meeting points."""
    fallback = {
        "teams": {
            str(_as_int(team.get("id")) or index): (
                "Baskı akışı, takımın öne çıktığı ve oyunu daha çok rakibe bıraktığı dakikaları gösteriyor."
                if lang == "tr" else "The pressure flow identifies the minutes when the team steps forward or cedes more of the game."
            )
            for index, team in enumerate(teams)
        },
        "match_outlook": (
            "İki baskı eğiliminin kesiştiği dönemler, maçın kontrolünün en sık el değiştirebileceği aralıklar olacaktır."
            if lang == "tr" else "The periods where the two pressure trends meet should be the intervals in which control changes hands most often."
        ),
    }
    if len(usages) != 2 or not os.getenv("OPENAI_API_KEY"):
        return fallback
    evidence = [
        {"team_id": _as_int(team.get("id")), "team_name": team.get("name"), "momentum": usage.get("momentum") or []}
        for usage, team in zip(usages, teams)
    ]
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior pre-match football analyst. Return only valid JSON with exactly: {\"teams\": {\"team_id\": \"text\", ...}, \"match_outlook\": \"text\"}. Write one concise 25-40 word evidence-led interpretation per team from 10-minute net-pressure and goal patterns, plus one 25-40 word match-outlook sentence identifying likely pressure windows for the upcoming match. Use the requested language. Interpret, do not merely list data. No markdown, headings, bullets, leading dashes, invented certainty, or the Turkish word 'örneklem'."),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nPressure evidence: {json.dumps(evidence, ensure_ascii=False)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if isinstance(parsed, dict):
            returned_teams = parsed.get("teams") or {}
            if isinstance(returned_teams, dict):
                for team_id in fallback["teams"]:
                    text = str(returned_teams.get(team_id) or "").strip()
                    if text:
                        fallback["teams"][team_id] = text
            text = str(parsed.get("match_outlook") or "").strip()
            if text:
                fallback["match_outlook"] = text
    except Exception as exc:
        print(f"[pre_match_report] event=momentum_perspective_fallback error={exc}")
    return fallback


def _pre_match_score_flow_perspectives(
    usages: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    lang: str,
) -> dict[str, dict[str, str]]:
    """Return one focused interpretation per side for each score state."""
    states = ("ahead", "level", "behind")
    fallback = {
        state: {
            str(_as_int(team.get("id")) or index): (
                "Bu skor durumundaki süre, geçişler ve sonuçlar takımın maç yönetimi eğilimini gösteriyor."
                if lang == "tr" else "The time, transitions and outcomes in this game state show the team's match-management tendency."
            )
            for index, team in enumerate(teams)
        }
        for state in states
    }
    if len(usages) != 2 or not os.getenv("OPENAI_API_KEY"):
        return fallback
    evidence = [
        {"team_id": _as_int(team.get("id")), "team_name": team.get("name"), "score_flow": usage.get("score_flow") or {}}
        for usage, team in zip(usages, teams)
    ]
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior pre-match football analyst. Return only valid JSON with exactly the keys ahead, level, behind. Each key maps team_id strings to one concise 20-32 word interpretation. For each state, infer the team's game-management tendency and the likely consequence in the upcoming match from duration, transitions, goal balance and final results. Prioritize the insight; do not restate the statistics or list values. Use the requested language. No markdown, headings, bullets, leading dashes, invented facts, certainty, or the Turkish word 'örneklem'."),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nScore-flow evidence: {json.dumps(evidence, ensure_ascii=False)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if isinstance(parsed, dict):
            for state in states:
                returned_state = parsed.get(state) or {}
                if not isinstance(returned_state, dict):
                    continue
                for team_id in fallback[state]:
                    text = str(returned_state.get(team_id) or "").strip()
                    if text:
                        fallback[state][team_id] = text
    except Exception as exc:
        print(f"[pre_match_report] event=score_flow_perspective_fallback error={exc}")
    return fallback


def _pre_match_overview_summary(
    usages: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    team_analysis: dict[str, dict[str, str]],
    momentum: dict[str, Any],
    score_flow: dict[str, dict[str, str]],
    regional: str,
    comparisons: dict[str, str],
    lang: str,
) -> list[dict[str, Any]]:
    """Create the pre-match equivalent of the post-match report summary."""
    tr = lang == "tr"
    first = usages[0] if usages else {}
    second = usages[1] if len(usages) > 1 else {}
    first_name = str((teams[0] if teams else {}).get("name") or ("Ev sahibi" if tr else "Home side"))
    second_name = str((teams[1] if len(teams) > 1 else {}).get("name") or ("Deplasman" if tr else "Away side"))
    first_id = str(_as_int((teams[0] if teams else {}).get("id")) or "")
    second_id = str(_as_int((teams[1] if len(teams) > 1 else {}).get("id")) or "")
    analysis_first = team_analysis.get(first_id, {})
    analysis_second = team_analysis.get(second_id, {})
    momentum_teams = momentum.get("teams") or {}
    formations = [
        ", ".join(str(row.get("formation") or "") for row in (usage.get("formations") or [])[:2] if row.get("formation"))
        for usage in (first, second)
    ]
    rotation_levels = []
    for usage in (first, second):
        try:
            rotation_levels.append(float((usage.get("summary") or {}).get("rotation_level") or 0))
        except (TypeError, ValueError):
            rotation_levels.append(0.0)
    fallback = [
        {"category": "Kadro ve Diziliş" if tr else "Squad & Formation", "summary": (f"{first_name}, {formations[0] or 'son kullanılan düzeni'} etrafında yüzde {rotation_levels[0]:.1f} rotasyonla süreklilik ararken; {second_name}, {formations[1] or 'son kullanılan düzeni'} içinde yüzde {rotation_levels[1]:.1f} rotasyonla farklı bir kadro devamlılığı sergiliyor." if tr else f"{first_name} combines {formations[0] or 'its recent structure'} with {rotation_levels[0]:.1f}% rotation, while {second_name} uses {formations[1] or 'its recent structure'} with {rotation_levels[1]:.1f}% rotation, shaping two distinct continuity profiles."), "sub_bullets": [{"label": first_name, "text": formations[0] or ("Son kadro kullanımı değerlendirildi." if tr else "Recent squad usage assessed.")}, {"label": second_name, "text": formations[1] or ("Son kadro kullanımı değerlendirildi." if tr else "Recent squad usage assessed.")}]},
        {"category": "Form ve Sonuçlar" if tr else "Form & Results", "summary": (f"{first_name} ile {second_name} için son sonuçlar, maçın ritmine taşınabilecek form ve skor eğilimlerini ortaya koyuyor." if tr else f"Recent results for {first_name} and {second_name} reveal the form and scoring trends that may carry into the match."), "sub_bullets": []},
        {"category": "Göze Çarpan Oyuncular" if tr else "Standout Players", "summary": ("90 dakikanın üzerinde süre alan oyuncuların puan ve rol katkıları, iki takımın belirleyici bireysel kaynaklarını öne çıkarıyor." if tr else "Ratings and role output from players exceeding 90 minutes identify the key individual resources for both teams."), "sub_bullets": []},
        {"category": "Baskı Endeksi" if tr else "Pressure Index", "summary": str(momentum.get("match_outlook") or ""), "sub_bullets": [{"label": first_name, "text": str(momentum_teams.get(first_id) or "")}, {"label": second_name, "text": str(momentum_teams.get(second_id) or "")} ]},
        {"category": "Skor Akış Profili" if tr else "Score Flow Profile", "summary": ("Skor durumlarındaki geçişler, iki takımın avantajı koruma ve maç içinde dengeyi değiştirme eğilimlerini karşılaştırıyor." if tr else "Score-state transitions compare how the two sides protect advantages and alter balance during matches."), "sub_bullets": []},
        {"category": "Bölgesel Oyun Dağılımı" if tr else "Regional Play Distribution", "summary": regional, "sub_bullets": []},
        {"category": "Takım Karşılaştırması" if tr else "Team Comparison", "summary": next(iter(comparisons.values()), ""), "sub_bullets": []},
        {"category": "Takım Analizi" if tr else "Team Analysis", "summary": (f"{first_name}: {analysis_first.get('positive') or ''} {second_name}: {analysis_second.get('positive') or ''}").strip(), "sub_bullets": []},
    ]
    if not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        evidence = {
            "teams": teams,
            "recent_usage": usages,
            "team_analysis": team_analysis,
            "momentum": momentum,
            "score_flow": score_flow,
            "regional": regional,
            "comparisons": comparisons,
        }
        required = [str(row["category"]) for row in fallback]
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior pre-match report editor. Return only valid JSON: an array in the exact REQUIRED_CATEGORY order, each object exactly {category, summary, sub_bullets}. Write a 28-45 word insight-led summary for every section using the supplied evidence and existing ScoutWise conclusions. Do not describe the page, restate raw figures, use markdown, bullets, leading dashes, K1/K2, left/right, or mention unavailable information. Always name the teams instead of saying 'bir ekip', 'diğer ekip', 'bir taraf' or 'diğer taraf'. In Kadro ve Diziliş, explicitly compare both named teams' formation continuity, rotation_level, and the duration-weighted team average_rating; interpret how those three signals shape squad continuity. In Team Analysis, write one fluent comparison: name each team once and never repeat a team name back-to-back. For Regional Play Distribution, use neutral football language such as centre, wide corridors, central lanes and penalty-area proximity; never imply a fixed attacking direction. Add zero to two concise supporting sub_bullets only where they provide a distinct insight. Turkish must use natural Turkish football language and never use 'örneklem'."),
            ("human", f"Language: {'Turkish' if tr else 'English'}\nREQUIRED_CATEGORY: {json.dumps(required, ensure_ascii=False)}\nEvidence: {json.dumps(evidence, ensure_ascii=False, default=str)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if not isinstance(parsed, list):
            raise ValueError("pre-match overview response was not an array")
        by_category = {str(row.get("category") or ""): row for row in parsed if isinstance(row, dict)}
        rows = []
        for fallback_row in fallback:
            row = by_category.get(str(fallback_row["category"])) or {}
            summary = re.sub(r"^\s*(?:[-•–—]\s*)+", "", str(row.get("summary") or "").strip())
            bullets = [{"label": str(item.get("label") or "").strip(), "text": re.sub(r"^\s*(?:[-•–—]\s*)+", "", str(item.get("text") or "").strip())} for item in (row.get("sub_bullets") or [])[:2] if isinstance(item, dict) and str(item.get("text") or "").strip()]
            rows.append({"category": fallback_row["category"], "summary": summary or fallback_row["summary"], "sub_bullets": bullets})
        return rows
    except Exception as exc:
        print(f"[pre_match_report] event=overview_summary_fallback error={exc}")
        return fallback


def _pre_match_regional_perspective(usages: list[dict[str, Any]], teams: list[dict[str, Any]], lang: str) -> str:
    """Apply the match-report regional classification to each team sample.

    The three horizontal bands contain equal areas.  A band is only called
    clearly more intense when it is at least five percentage points ahead,
    preventing a minor distribution difference from becoming a tactical claim.
    """
    compact = []
    for usage, team in zip(usages, teams):
        zones = usage.get("territory") or []
        values = [
            float(next((row.get("percentage") for row in zones if _as_int(row.get("index")) == index), 0) or 0)
            for index in range(9)
        ]
        upper_band = round(sum(values[index] for index in (0, 1, 2)), 1)
        central_band = round(sum(values[index] for index in (3, 4, 5)), 1)
        lower_band = round(sum(values[index] for index in (6, 7, 8)), 1)
        margin = 5.0
        upper_clear = upper_band - central_band >= margin
        lower_clear = lower_band - central_band >= margin
        central_clear = central_band - upper_band >= margin and central_band - lower_band >= margin
        concentration = (
            "central_area_more_intense" if central_clear else
            "both_outer_bands_more_intense" if upper_clear and lower_clear else
            "one_outer_band_more_intense" if upper_clear or lower_clear else
            "balanced_no_clear_advantage"
        )
        compact.append({
            "team": team.get("name"),
            "zones": values,
            "semantic_spatial_aggregates": {
                "upper_band_pct": upper_band,
                "central_band_pct": central_band,
                "lower_band_pct": lower_band,
                "middle_field_band_pct": round(sum(values[index] for index in (1, 4, 7)), 1),
                "penalty_area_vicinity_proxy_pct": round(sum(values[index] for index in (0, 3, 6, 2, 5, 8)), 1),
                "minimum_clear_difference_percentage_points": margin,
                "channel_concentration": concentration,
            },
        })
    fallback = (
        "İki takımın bölgesel dağılımı, merkez ve geniş alanların kullanımındaki ortaklıkların topun dolaşımını şekillendireceğini gösteriyor. Ceza alanlarına yakın bölgelerdeki yoğunluklar ile merkez çevresindeki ikinci toplar, yerleşim mücadelesinin ana odağı olabilir."
        if lang == "tr" else
        "The two regional distributions indicate that central connections and concentration near the penalty areas will shape the ball's direction. Second balls and positional battles should stand out in the corridors where their shares overlap."
    )
    if len(compact) != 2 or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior pre-match spatial analyst. Write one 45-65 word ScoutWise perspective in the requested language. Use the same strict regional logic as the match report. The three supplied bands have equal area. Obey each team's channel_concentration exactly: central_area_more_intense means the central band exceeds both outer bands by at least five percentage points; both_outer_bands_more_intense means both outer bands exceed the centre by at least five points; one_outer_band_more_intense means only one does; balanced_no_clear_advantage means do not claim clear centre-versus-wide superiority. Never compare both outer bands combined against the central band. The band calculation is internal: never mention upper, lower, top, bottom, vertical lanes, zone numbers, or any coordinate wording in the output. Compare the two teams only with symmetric football language such as centre, wide areas, central connections and penalty-area proximity. Identify overlaps or contrasts and infer where the ball is likely to be contested. Never use K1, K2, left, right, home side, away side, or any fixed attacking direction. Do not merely list percentages, invent certainty, tactics, unavailable facts, or use the Turkish word 'örneklem'. No markdown or heading."),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nRegional distributions: {json.dumps(compact, ensure_ascii=False)}"),
        ])
        text = str(response.content or "").strip()
        # The UI owns the ScoutWise Perspektifi heading.  Keep generated copy
        # to the spatial reading only, even when the model adds a heading.
        text = re.sub(
            r"^\s*(?:scoutwise\s+(?:perspektifi|perspective)\s*:\s*)+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        return text or fallback
    except Exception as exc:
        print(f"[pre_match_report] event=regional_perspective_fallback error={exc}")
        return fallback


def build_recent_squad_usage(fixture: dict[str, Any], lang: str = "en") -> dict[str, Any]:
    """Aggregate each side's most recent completed matches in the fixture season.

    Each team is intentionally sampled independently: five completed matches if
    available, otherwise every completed match from that season.
    """
    season_id = _as_int((fixture.get("season") or {}).get("id"))
    season_name = str((fixture.get("season") or {}).get("name") or "")
    competition_id = _as_int((fixture.get("league") or {}).get("id"))
    reference_date = str(fixture.get("startingAt") or "")
    teams = [dict(fixture.get("homeTeam") or {}), dict(fixture.get("awayTeam") or {})]
    with ThreadPoolExecutor(max_workers=2) as executor:
        usages = list(
            executor.map(
                lambda team: _team_recent_squad_usage(team, season_id, season_name, competition_id, reference_date),
                teams,
            )
        )
    team_analysis = _pre_match_team_analysis(usages, teams, lang)
    comparisons = _pre_match_team_comparison_perspectives(usages, teams, lang)
    momentum = _pre_match_momentum_perspectives(usages, teams, lang)
    score_flow = _pre_match_score_flow_perspectives(usages, teams, lang)
    regional = _pre_match_regional_perspective(usages, teams, lang)
    return {
        "teams": usages,
        "head_to_head": _head_to_head_results(teams[0], teams[1], season_id, reference_date),
        "player_perspectives": _pre_match_player_perspectives(usages, lang),
        "team_analysis": team_analysis,
        "team_comparison_perspectives": comparisons,
        "momentum_perspectives": momentum,
        "score_flow_perspectives": score_flow,
        "regional_perspective": regional,
        "overview_summary": _pre_match_overview_summary(usages, teams, team_analysis, momentum, score_flow, regional, comparisons, lang),
    }


def build_pre_match_analysis(fixture: dict[str, Any], lang: str = "en") -> dict[str, Any]:
    """Build the initial pre-match report payload for a scheduled fixture.

    The current payload is deliberately limited to the fixture card. Subsequent
    pre-match sections should be added here rather than to the post-match report
    generator, whose inputs require completed-match events and statistics.
    """
    return {
        "report_type": "pre_match",
        "language": lang,
        "fixture": fixture,
    }
