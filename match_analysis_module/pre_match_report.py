"""ScoutWise Enterprise pre-match analysis module.

This module is intentionally separate from the completed-match report pipeline.
It will own the data collection, aggregation and report construction required for
Maç Önü Analizi as the report sections are introduced.
"""

from __future__ import annotations

from report_module.penalty_xg import PENALTY_XG, penalty_xg_sample, penalty_xg_average
from report_module.progress import emit_progress
from report_module.terminology import FOOTBALL_TERMINOLOGY_GUIDANCE

from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Any

import requests

from report_module.rate_evidence import missing_attempt_names, key as rate_key
from report_module.metric_validation import is_standard_count, nonzero_report_metric_evidence, percentage, pooled_rate_counts, valid_metric

from .match_analysis import SPORTMONKS_BASE_URL, SportMonksError
from .regional_play import aggregate_regional_play, regional_perspective
from match_report_module.report import DERIVED_PERCENTAGE_METRICS, PLAYER_METRIC_CATEGORY, TEAM_METRIC_CATEGORY, _categorize, _derive_team_percentages, _metric, _value


PRE_MATCH_SCHEMA_VERSION = 13

_COMPLETED_STATES = {"FT", "AET", "PEN", "WO", "AP"}


def has_recent_squad_usage(content: dict[str, Any]) -> bool:
    """Distinguish a saved historical sample from a swallowed provider failure."""
    usage = content.get("recent_squad_usage") or {}
    return bool(usage.get("teams"))


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
    """Return the ten latest completed matches from the active season."""
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
    # keeps domestic-league and European matches in one latest-ten sequence.
    source_by_id = {
        _as_int(item.get("id")): item
        for item in [*season_fixtures, *same_named_season_fixtures]
        if _as_int(item.get("id"))
    }
    source = list(source_by_id.values())
    return sorted(source, key=lambda item: item.get("starting_at") or "", reverse=True)[:10]


def _fixture_lineup_usage(fixture_id: int, team_id: int, include: str | None = None) -> dict[str, Any]:
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/fixtures/{fixture_id}",
        params={
            "api_token": token,
            "include": include or "participants;formations;lineups.player;lineups.position;lineups.details.type;lineups.xGLineup.type;events.type;pressure;ballCoordinates;periods.type;statistics.type;xGFixture.type;stage",
        },
        timeout=15 if include else 35,
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
_PLAYER_METRIC_MIN_RECORDED_MINUTES = 90
_PLAYER_HIGHLIGHT_EXCLUDED_METRICS = {
    "Long Balls Won Percentage",
    "Long Balls Won (%)",
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
_DERIVED_PLAYER_PERCENTAGES = DERIVED_PERCENTAGE_METRICS

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
            and valid_metric(name, value)
            and name in PLAYER_METRIC_CATEGORY
            and name.casefold() not in _PLAYER_INSIGHT_EXCLUDED_METRICS
            and is_standard_count(PLAYER_METRIC_CATEGORY[name][1], PLAYER_METRIC_CATEGORY[name][0])
            and "%" not in name
            and not any(term in name.casefold() for term in _PLAYER_RATE_METRIC_TERMS)
        ):
            canonical = PLAYER_METRIC_CATEGORY[name][1]
            values[canonical] = values.get(canonical, 0.0) + value
    return values


def _match_covered_count_metrics(row: dict[str, Any], covered_names: set[str]) -> dict[str, float]:
    """Zero-fill omitted counts only when another player records them in this match.

    Explicit null/invalid values, advanced metrics and percentages stay unknown.
    """
    values = _lineup_numeric_metrics(row)
    present = {
        PLAYER_METRIC_CATEGORY.get(_PLAYER_METRIC_NAME_ALIASES.get(name, name), (None, name))[1]
        for detail in row.get("details") or []
        for name in [str((detail.get("type") or {}).get("name") or detail.get("name") or "").strip()]
    }
    protected = missing_attempt_names(values)
    for name in sorted(covered_names - present):
        if rate_key(name) not in protected:
            values[name] = 0.0
    return values


def _lineup_rate_inputs(row):
    values = _lineup_numeric_metrics(row)
    for detail in [*(row.get("xglineup") or row.get("xGLineup") or []), *(row.get("details") or [])]:
        name = str((detail.get("type") or {}).get("name") or detail.get("name") or "")
        if rate_key(name) not in {"expected_goals", "expected_goals_on_target"}:
            continue
        raw = detail.get("data")
        raw = raw.get("value") if isinstance(raw, dict) else raw
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            values[name] = value
    return values


def _lineup_rate_metrics(row: dict[str, Any]) -> dict[str, float]:
    """Canonicalise provider rates; known count-based rates are pooled separately."""
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
            and valid_metric(name, value)
            and name in PLAYER_METRIC_CATEGORY
            and ("%" in name or any(term in name.casefold() for term in _PLAYER_RATE_METRIC_TERMS))
        ):
            values[PLAYER_METRIC_CATEGORY[name][1]] = value
    return values


def _highlight_metric_eligible(metric: dict[str, Any], sample_size: int) -> bool:
    return int(metric.get("matches_covered") or 0) * 5 >= max(1, sample_size)


def _interpretation_metrics(player, selection, sample_size):
    field = 'development_metrics' if selection == 'development' else 'standout_metrics'
    return [metric for metric in player.get(field) or []
            if metric.get('name') not in _PLAYER_HIGHLIGHT_EXCLUDED_METRICS
            and str(metric.get('name') or '').casefold() not in _PLAYER_INSIGHT_EXCLUDED_METRICS
            and valid_metric(str(metric.get('name') or ''), metric.get('value'), nonzero=True)
            and float(metric.get('recorded_minutes') or 0) >= _PLAYER_METRIC_MIN_RECORDED_MINUTES
            and _highlight_metric_eligible(metric, sample_size)]


def _can_interpret_player(player, selection, sample_size):
    names = {str(metric['name']).casefold() for metric in _interpretation_metrics(player, selection, sample_size)}
    # Average rating is displayed separately and never counts as evidence.
    return len(names) >= (2 if selection == 'development' else 1)


def _distinct_player_highlight_metrics(metrics):
    """Keep one chance-creation indicator and fill with other eligible outputs."""
    selected = []
    used = set()
    creation = {'chances created', 'big chances created', 'key passes'}
    for metric in metrics:
        name = str(metric.get('name') or '').strip().casefold()
        family = 'chance_creation' if name in creation else name
        if not family or family in used:
            continue
        used.add(family)
        selected.append(metric)
        if len(selected) == 3:
            break
    return selected


def _attach_player_highlight_metrics(players: list[dict[str, Any]], sample_size: int) -> None:
    """Pick three role-output metrics from each eligible player's per-90 sample.

    Positive selections are the player's strongest relative outputs in the
    selected team sample. Development selections deliberately use the lowest
    relative productive outputs, never the player's strongest numbers.
    """
    metric_population: dict[str, list[float]] = defaultdict(list)
    for player in players:
        for metric in player.get("per90_metrics") or []:
            if _highlight_metric_eligible(metric, sample_size):
                metric_population[str(metric["name"])].append(float(metric["value"]))

    for player in players:
        scored = []
        risk_scored = []
        for metric in player.get("per90_metrics") or []:
            if (not _highlight_metric_eligible(metric, sample_size)
                or str(metric.get("name") or "") in _PLAYER_HIGHLIGHT_EXCLUDED_METRICS
                or str(metric.get("name") or "").casefold() in _PLAYER_INSIGHT_EXCLUDED_METRICS):
                continue
            values = metric_population.get(str(metric["name"]), [])
            if not values:
                continue
            value = float(metric["value"])
            if not valid_metric(str(metric["name"]), value, nonzero=True):
                continue
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
        player["standout_metrics"] = _distinct_player_highlight_metrics(sorted(
            scored, key=lambda item: (-item["relative_score"], -item["value"], item["name"])
        ))
        # Lead with errors/discipline where that player's selected-match
        # record provides it. When none is recorded, show the lowest
        # productive outputs rather than inventing a risk.
        player["development_metrics"] = (
            _distinct_player_highlight_metrics(sorted(risk_scored, key=lambda item: (-item["relative_score"], -item["value"], item["name"])))
            or _distinct_player_highlight_metrics(sorted(scored, key=lambda item: (item["relative_score"], item["value"], item["name"])))
        )


def _select_player_highlights(players, sample_size):
    _attach_player_highlight_metrics(players, sample_size)
    rating_order = sorted(
        [player for player in players if _interpretation_metrics(player, 'featured', sample_size)],
        key=lambda item: (-item["average_rating"], -item["minutes"], item["player_name"]),
    )
    goal_order = sorted(
        [player for player in players if _interpretation_metrics(player, 'featured', sample_size)],
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
    # Prefer two distinct standout players when sufficient evidence exists. Prefer the
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
        player for player in players
        if _can_interpret_player(player, 'development', sample_size)
        and str(player.get("position_name") or "").strip().casefold()
        not in {"goalkeeper", "kaleci"}
    ]
    development = sorted(
        outfield_performers,
        key=lambda item: (item["average_rating"], -item["minutes"], item["player_name"]),
    )[0] if outfield_performers else None
    return {
        "top_rated": top_rated, "top_scorer": top_scorer, "top_scorers": top_scorers,
        "featured_players": featured_players, "development": development,
    }


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
    fixtures: Iterable[dict[str, Any]],
    team_id: int,
) -> dict[str, list[dict[str, Any]]]:
    """Consume selected fixtures once, retaining totals instead of payloads.

    Standard counts include omitted zero events across all selected fixtures.
    Advanced values retain their recorded-match denominator. Known rate
    metrics are recalculated from their aggregate numerator and denominator;
    other provider rates are averaged. Keeping matches_covered per metric makes
    the comparison robust when the provider omits a metric in part of a sample.
    """
    from report_module.rate_evidence import empty, add_sample, rate_value
    from standings_module.metrics import normalize_fixture
    from .pre_match_team_metrics import extend_comparison

    samples = defaultdict(dict)
    rates = empty()
    protected = set()
    penalty_samples = []
    def normalized_rows():
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
            fixture_values = {}
            for group, metrics in {**categories, "expected": expected}.items():
                for metric in metrics:
                    try:
                        value = float(metric.get("value"))
                    except (TypeError, ValueError):
                        continue
                    name = str(metric.get("name") or "").strip()
                    if name and valid_metric(name, value):
                        state = samples[group].setdefault(name, [0., 0])
                        state[0] += value
                        state[1] += 1
                        fixture_values.setdefault(group, {})[name] = value
            penalty_sample = penalty_xg_sample(fixture_values.get("expected", {}).get(PENALTY_XG), fixture.get("events") or [], team_id)
            if penalty_sample is not None:
                penalty_samples.append(penalty_sample)
            flat = {name: value for items in fixture_values.values() for name, value in items.items()}
            protected.update(missing_attempt_names(flat))
            add_sample(rates, flat)
            yield normalize_fixture({**fixture, "lineups": []})
    additions = extend_comparison({}, normalized_rows(), team_id)
    aggregate: dict[str, list[dict[str, Any]]] = {}
    for group, metrics in samples.items():
        rows = []
        for name, (total, covered) in metrics.items():
            if rate_key(name) in protected:
                continue
            if group == "expected" and name == PENALTY_XG:
                average = penalty_xg_average(penalty_samples)
                if average is not None:
                    rows.append({"name": name, "value": average, "total": average,
                                 "aggregation": "average", "matches_covered": len(penalty_samples),
                                 "attempts": sum(count for _, count in penalty_samples)})
                continue
            average = total / covered
            # Averaging an index does not make its unit a percentage.
            is_average = "%" in name or "percentage" in name.casefold() or "performance" in name.casefold()
            numerator_denominator = DERIVED_PERCENTAGE_METRICS.get(name)
            state = rates['rates'].get(rate_key(name)) if numerator_denominator else None
            derived_rate = rate_value(name, state) if numerator_denominator else None
            if numerator_denominator and derived_rate is None:
                continue
            rows.append({
                "name": name,
                # Per-match is the comparison default for multi-match samples.
                "value": round(derived_rate, 2) if derived_rate is not None else round(average, 2),
                "total": round(derived_rate if derived_rate is not None else average if is_average else total, 2),
                "aggregation": "derived_rate" if derived_rate is not None else "average" if is_average else "per_match",
                "matches_covered": int(state["coverage"]) if derived_rate is not None else covered,
            })
        if rows:
            aggregate[group] = sorted(rows, key=lambda row: row["name"])
    replaced = {row['name'] for rows in additions.values() for row in rows}
    for group, rows in aggregate.items():
        additions.setdefault(group, []).extend(row for row in rows if row['name'] not in replaced)
    return {group: sorted(rows, key=lambda row: row['name']) for group, rows in additions.items() if rows}


def _aggregate_score_flow(fixtures: list[dict[str, Any]], team_id: int) -> dict[str, Any]:
    """Build the same ahead/level/behind state model used in team analysis."""
    state_order = ("ahead", "level", "behind")
    totals = {
        key: {"minutes": 0.0, "segments": 0, "matches_entered": 0, "goals_for": 0, "goals_against": 0, "to_ahead": 0, "to_level": 0, "to_behind": 0, "matches_to_ahead": 0, "matches_to_level": 0, "matches_to_behind": 0, "final_wins": 0, "final_draws": 0, "final_losses": 0}
        for key in state_order
    }

    def game_state(team_score: int, opponent_score: int) -> str:
        return "ahead" if team_score > opponent_score else "behind" if team_score < opponent_score else "level"

    seen_fixtures = set()
    for fixture in fixtures:
        fid = fixture.get("id")
        if fid is not None and fid in seen_fixtures:
            continue
        if fid is not None:
            seen_fixtures.add(fid)
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
        transitions = set()
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
                transitions.add((current, next_state))
                totals[next_state]["segments"] += 1
                entered.add(next_state)
                current = next_state
            cursor = minute
        totals[current]["minutes"] += max(0.0, duration - cursor)
        for origin, target in transitions:
            totals[origin][f"matches_to_{target}"] += 1
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
                "segments": int(totals[key]["segments"]),
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
    *,
    _comparison_fixtures: list[dict[str, Any]] | None = None,
    _comparison_player_ids: set[int] | None = None,
    _include_finding_snapshots: bool = False,
) -> dict[str, Any]:
    team_id = _as_int(team.get("id"))
    fallback = {
        "team_id": team_id,
        "sample_size": 0,
        "current_squad_count": 0,
        "players": [],
        "formations": [],
        **aggregate_regional_play([], team_id, str(team.get("location") or "home")),
    }
    if not team_id:
        return fallback
    current_player_ids = _comparison_player_ids if _comparison_player_ids is not None else _current_team_player_ids(team_id)
    fixtures = _comparison_fixtures if _comparison_fixtures is not None else _recent_team_fixtures(team_id, season_id, season_name, competition_id, reference_date)
    if not fixtures:
        return fallback
    fixture_ids = [_as_int(item.get("id")) for item in fixtures if _as_int(item.get("id"))]
    if _comparison_fixtures is not None:
        full_fixtures = _comparison_fixtures
    else:
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
    player_metric_matches: dict[int, Counter[str]] = defaultdict(Counter)
    player_rate_weighted_totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    player_rate_minutes: dict[int, Counter[str]] = defaultdict(Counter)
    player_rate_matches: dict[int, Counter[str]] = defaultdict(Counter)
    player_derived_matches: dict[int, Counter[str]] = defaultdict(Counter)
    player_count_samples: dict[int, list[tuple[dict[str, float], float]]] = defaultdict(list)
    player_derived_rates: dict[int, dict[str, tuple[float, float, float]]] = defaultdict(dict)
    pressure_samples: dict[int, list[float]] = {}
    pressure_by_match: list[dict[int, float]] = []
    momentum_events: list[dict[str, Any]] = []
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

        covered_count_names = {
            name for lineup in fixture.get("lineups") or []
            if (_metric_value(lineup, "Minutes Played") or 0) > 0
            for name in _lineup_numeric_metrics(lineup)
        }
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
                count_metrics = _match_covered_count_metrics(row, covered_count_names)
                for metric_name, metric_value in count_metrics.items():
                    player_metric_totals[player_id][metric_name] += metric_value
                    # Include recorded and peer-covered omitted counts only.
                    player_metric_minutes[player_id][metric_name] += minutes
                    player_metric_matches[player_id][metric_name] += 1
                player_count_samples[player_id].append((_lineup_rate_inputs(row), minutes))
            # Rates without known count components retain the provider average.
            if minutes > 0:
                for metric_name, metric_value in _lineup_rate_metrics(row).items():
                    if metric_name in _DERIVED_PLAYER_PERCENTAGES:
                        continue
                    player_rate_weighted_totals[player_id][metric_name] += metric_value * minutes
                    player_rate_minutes[player_id][metric_name] += minutes
                    player_rate_matches[player_id][metric_name] += 1

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
        match_pressure = {minute: own_pressure[minute] - opponent_pressure[minute]
                          for minute in own_pressure.keys() | opponent_pressure.keys()}
        if match_pressure:
            pressure_by_match.append(match_pressure)
        for minute, value in match_pressure.items():
            pressure_samples.setdefault(minute, []).append(value)

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
    for player_id, count_samples in player_count_samples.items():
        for rate_name, components in _DERIVED_PLAYER_PERCENTAGES.items():
            pooled = pooled_rate_counts(rate_name, count_samples, *components)
            if pooled is not None:
                player_derived_rates[player_id][rate_name] = pooled
                # A percentage is observed only where its denominator is positive.
                # Zero successes with recorded attempts remain valid observations.
                player_derived_matches[player_id][rate_name] = sum(
                    single is not None and single[1] > 0
                    for counts, _ in count_samples
                    for single in [pooled_rate_counts(rate_name, [(counts, 1)], *components)]
                )
    eligible_performers = []
    comparison_performers = []
    for player_id, player in current_players.items():
        minutes = float(player_minutes[player_id])
        rating_minutes = float(player_rating_minutes[player_id])
        if minutes <= 0:
            continue
        performer = {
            **player,
            "minutes": round(minutes),
            "average_rating": round(float(player_rating_weighted[player_id]) / rating_minutes, 2) if rating_minutes else None,
            "appearances": int(starts[player_id] + substitute_appearances[player_id]),
            "goals": int(player_goals[player_id]),
            "per90_metrics": [
                {
                    "name": name,
                    "value": round(value * 90 / player_metric_minutes[player_id][name], 2),
                    "is_percentage": False,
                    "recorded_minutes": player_metric_minutes[player_id][name],
                    "matches_covered": player_metric_matches[player_id][name],
                }
                for name, value in player_metric_totals[player_id].items()
                if player_metric_minutes[player_id][name] >= _PLAYER_METRIC_MIN_RECORDED_MINUTES and value >= 0
                and not any(rate_key(name) in missing_attempt_names(counts) for counts, _ in player_count_samples[player_id])
            ] + [
                {
                    "name": name,
                    "value": round(numerator * 100 / denominator, 2),
                    "recorded_minutes": covered_minutes,
                    "matches_covered": player_derived_matches[player_id][name],
                    "is_percentage": True,
                }
                for name, (numerator, denominator, covered_minutes) in player_derived_rates[player_id].items()
                if denominator > 0 and covered_minutes >= _PLAYER_METRIC_MIN_RECORDED_MINUTES
            ] + [
                {
                    "name": name,
                    "value": round(weighted_value / player_rate_minutes[player_id][name], 2),
                    "recorded_minutes": player_rate_minutes[player_id][name],
                    "matches_covered": player_rate_matches[player_id][name],
                    "is_percentage": True,
                }
                for name, weighted_value in player_rate_weighted_totals[player_id].items()
                if player_rate_minutes[player_id][name] >= _PLAYER_METRIC_MIN_RECORDED_MINUTES
            ],
        }
        comparison_performers.append(performer)
        if minutes > 90 and rating_minutes:
            eligible_performers.append(performer)
    from .pre_match_player_comparison import append_comparison_role_metrics
    comparison_performers = [{**player, "per90_metrics": list(player["per90_metrics"])} for player in comparison_performers]
    append_comparison_role_metrics(comparison_performers, full_fixtures, team_id)
    if _comparison_fixtures is not None:
        return {"comparison_players": comparison_performers, "selected_fixture_ids": fixture_ids, "sample_size": len(fixtures)}
    comparison_ids = fixture_ids
    performance_summary = _select_player_highlights(eligible_performers, len(fixtures))
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
        match_windows = [[value for minute, value in match.items() if minute >= start and (end is None or minute <= end)]
                         for match in pressure_by_match]
        covered = [values for values in match_windows if values]
        momentum_rows.append({
            "pressure_matches": len(covered),
            "positive_pressure_matches": sum(sum(values) > 0 for values in covered),
            "period": "90+" if end is None else f"{start}-{end}",
            "average_net_pressure": round(sum(pressure_values) / len(pressure_values), 2) if pressure_values else 0,
            "goals_for": sum(event["goals_for"] for event in scoped_events),
            "goals_against": sum(event["goals_against"] for event in scoped_events),
        })
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
    if _include_finding_snapshots:
        from .pre_match_competition_findings import finding_snapshots
        scoped_inputs = {'_finding_snapshots': finding_snapshots(fixtures, full_fixtures)}
    else:
        scoped_inputs = {}
    return {
        **scoped_inputs,
        "team_id": team_id,
        "sample_size": sample_size,
        "current_squad_count": len(current_player_ids),
        "selected_fixture_ids": fixture_ids,
        "comparison_players": comparison_performers,
        "comparison_selected_fixture_ids": comparison_ids,
        "comparison_sample_size": len(comparison_ids),
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
        "performance_summary": performance_summary,
        "momentum": momentum_rows,
        **aggregate_regional_play(full_fixtures, team_id, str(team.get("location") or "home")),
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


def _pre_match_overview_and_plans(
    usages: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    team_findings: dict[str, Any],
    regional: str,
    lang: str,
    player_comparison: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Draft both the report overview and the two team plans in one call."""
    tr = lang == "tr"
    first = usages[0] if usages else {}
    second = usages[1] if len(usages) > 1 else {}
    first_name = str((teams[0] if teams else {}).get("name") or ("Ev sahibi" if tr else "Home side"))
    second_name = str((teams[1] if len(teams) > 1 else {}).get("name") or ("Deplasman" if tr else "Away side"))
    first_id = str(_as_int((teams[0] if teams else {}).get("id")) or "")
    second_id = str(_as_int((teams[1] if len(teams) > 1 else {}).get("id")) or "")
    finding_summary = []
    for team_id, name in ((first_id, first_name), (second_id, second_name)):
        strengths = [card["title"] for card in (team_findings.get(team_id) or {}).get("strength", [])]
        if strengths:
            finding_summary.append(f"{name}: {', '.join(strengths)}.")
    momentum_bullets = []
    for name, usage in ((first_name, first), (second_name, second)):
        positive = [row for row in usage.get("momentum", [])
                    if row.get("pressure_matches") != 0 and (row.get("average_net_pressure") or 0) > 0]
        if positive:
            peak = max(positive, key=lambda row: row["average_net_pressure"])
            phrase = (f"Son maçlarda en yüksek ortalama baskı {peak['period']}. dakika aralığında görüldü."
                      if tr else f"The highest average pressure in recent matches came in the {peak['period']}-minute interval.")
            momentum_bullets.append({"label": name, "text": phrase})
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
        {"category": "Baskı Endeksi" if tr else "Pressure Index",
         "summary": (f"{first_name} ve {second_name} için baskı profili, son maçlardaki on dakikalık dilimlere dayanıyor."
                     if tr else f"The pressure profiles of {first_name} and {second_name} describe ten-minute intervals in their recent matches."),
         "sub_bullets": momentum_bullets},
        {"category": "Skor Akış Profili" if tr else "Score Flow Profile", "summary": ("Skor durumlarındaki geçişler, iki takımın avantajı koruma ve maç içinde dengeyi değiştirme eğilimlerini karşılaştırıyor." if tr else "Score-state transitions compare how the two sides protect advantages and alter balance during matches."), "sub_bullets": []},
        {"category": "Bölgesel Oyun Dağılımı" if tr else "Regional Play Distribution", "summary": regional, "sub_bullets": []},
        {"category": "Takım Karşılaştırması" if tr else "Team Comparison",
         "summary": (f"{first_name} ve {second_name} karşılaştırmasında son maç değerleri ile lig sıralaması farklı dönemleri gösteriyor."
                     if tr else f"The recent-match values and league rankings for {first_name} and {second_name} describe different samples."),
         "sub_bullets": []},
        {"category": "Takım Analizi" if tr else "Team Analysis", "summary": " ".join(finding_summary), "sub_bullets": []},
    ]
    from .pre_match_player_comparison import (PLAYER_OVERVIEW_VERSION, localize_overview_text,
        player_comparison_overview, player_comparison_overview_evidence, player_overview_mentions_player)
    fallback.append(player_comparison_overview({'teams': usages, 'player_comparison': player_comparison or {}}, teams, lang))
    if not os.getenv("OPENAI_API_KEY"):
        return fallback, []
    try:
        from langchain_openai import ChatOpenAI
        from .score_flow_rates import rate_evidence
        from .pre_match_findings import TACTICAL_PLAN_GUIDANCE

        recent_usage = []
        for usage in usages:
            summary = usage.get("performance_summary") or {}
            featured = summary.get("featured_players") or [
                player for player in (summary.get("top_rated"), summary.get("top_scorer")) if player
            ]
            development = summary.get("development") or {}
            recent_usage.append({
                "team_id": usage.get("team_id"),
                "sample_size": usage.get("sample_size"),
                "summary": usage.get("summary"),
                "formations": usage.get("formations"),
                "results": usage.get("results"),
                "standout_players": [
                    {key: player.get(key) for key in ("player_name", "minutes", "average_rating", "goals", "standout_metrics")}
                    for player in featured[:2]
                ],
                "development_player": ({key: development.get(key) for key in
                                        ("player_name", "minutes", "average_rating", "development_metrics")}
                                       if development else None),
            })
        evidence = {
            "teams": nonzero_report_metric_evidence(teams),
            "recent_usage": recent_usage,
            "team_analysis": {"selected_findings": team_findings},
            "momentum": [{"team_id": usage.get("team_id"), "windows": usage.get("momentum") or []} for usage in usages],
            "score_flow": [{"team_id": usage.get("team_id"),
                            "states": rate_evidence((usage.get("score_flow") or {}).get("states") or [], snake=True)}
                           for usage in usages],
            "comparisons": [{"team_id": usage.get("team_id"), "groups": {
                group: [{key: row[key] for key in ("name", "value", "matches_covered", "preference") if key in row}
                        for row in rows if valid_metric(str(row.get("name") or ""), row.get("value"))]
                for group, rows in (usage.get("team_comparison") or {}).items()
            }} for usage in usages],
            "regional": regional,
            "player_comparison": player_comparison_overview_evidence({"teams": usages, "player_comparison": player_comparison or {}}, teams, lang),
        }
        required = [str(row["category"]) for row in fallback]
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior pre-match report editor. Return a JSON object with two keys. overview is an array in the exact REQUIRED_CATEGORY order, each object exactly {category, summary, sub_bullets}. tactical_plans is an array with one object {team_id, strategy} for each team. Use team_analysis.selected_findings for the tactical plans. Treat metrics marked context_only as context, never as evidence for a finding. Omit the tactical_plans entries if that evidence is empty. Write a 30-35 word insight-led summary for every section using the supplied evidence. For Pressure Index, base the summary on the recorded ten-minute windows and their match coverage where available. Treat historical patterns as watch windows, not forecasts. For Score Flow, compare conditional rates and sample sizes; winning after leading does not prove the lead was held continuously. For Team Comparison, use the raw paired recent-match metrics and distinguish opponent outputs from the team's own production. Do not describe the page, restate raw figures, use markdown, bullets, leading dashes, K1/K2, or mention unavailable information. Always name the teams instead of saying 'bir ekip', 'diğer ekip', 'bir taraf' or 'diğer taraf'. In Kadro ve Diziliş, explicitly compare both named teams' formation continuity, rotation_level, and the duration-weighted team average_rating; interpret how those three signals shape squad continuity. In Team Analysis, write one fluent comparison: name each team once and never repeat a team name back-to-back. In Player Comparison, compare the complete player pools of the named teams using the supplied anonymous role-level distributions. Never name, select or compare individual players, including the default selections. Use only player_comparison evidence for this section, not individual names or statistics from standout-player or lineup evidence. Role-level medians describe player groups, not team totals or season ranks. Respect lower-is-better errors and discipline. Explain the likely matchup implication of the shared role contributions without reciting values, and never imply selected matches include only the selected competition. Write a general comparison rather than instructions for using the selector; keep sub_bullets empty for this section. For Regional Play Distribution, use the supplied direction-adjusted historical evidence: the upcoming home team attacks left to right and the away team right to left. Name specific defensive, midfield or attacking channels and each named team's own left/right when supported. Compare where the distributions overlap or differ. Ball locations include both teams in each historical match; never infer possession ownership, playing style, defensive weakness, or guaranteed future tactics from location alone. Add zero to two concise supporting sub_bullets only where they provide a distinct insight. Turkish must use natural Turkish football language and never use 'örneklem'. In Turkish, always write '90 dakika başına' instead of 'per-90', 'per90' or 'per 90'." + FOOTBALL_TERMINOLOGY_GUIDANCE + " Tactical plan requirements: " + TACTICAL_PLAN_GUIDANCE),
            ("human", f"Language: {'Turkish' if tr else 'English'}\nREQUIRED_CATEGORY: {json.dumps(required, ensure_ascii=False)}\nEvidence: {json.dumps(evidence, ensure_ascii=False, default=str)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if not isinstance(parsed, dict):
            raise ValueError("pre-match overview and plans response was not an object")
        overview = parsed.get("overview")
        plans = parsed.get("tactical_plans")
        if not isinstance(overview, list):
            return fallback, plans if isinstance(plans, list) else []
        by_category = {str(row.get("category") or ""): row for row in overview if isinstance(row, dict)}
        rows = []
        for fallback_row in fallback:
            row = by_category.get(str(fallback_row["category"])) or {}
            summary = re.sub(r"^\s*(?:[-•–—]\s*)+", "", str(row.get("summary") or "").strip())
            bullets = [{"label": str(item.get("label") or "").strip(), "text": re.sub(r"^\s*(?:[-•–—]\s*)+", "", str(item.get("text") or "").strip())} for item in (row.get("sub_bullets") or [])[:2] if isinstance(item, dict) and str(item.get("text") or "").strip()]
            summary = localize_overview_text(summary or fallback_row["summary"], lang)
            bullets = [{**bullet, 'label': localize_overview_text(bullet['label'], lang),
                       'text': localize_overview_text(bullet['text'], lang)} for bullet in bullets]
            row = {"category": fallback_row["category"], "summary": summary, "sub_bullets": bullets}
            if fallback_row["category"] in {"Oyuncu Karşılaştırması", "Player Comparison"}:
                if player_overview_mentions_player(summary, {'teams': usages, 'player_comparison': player_comparison or {}}):
                    row = dict(fallback_row)
                row['sub_bullets'] = []
                row['player_overview_version'] = PLAYER_OVERVIEW_VERSION
            rows.append(row)
        return rows, plans if isinstance(plans, list) else []
    except Exception as exc:
        print(f"[pre_match_report] event=overview_summary_fallback error={exc}")
        return fallback, []


def _pre_match_regional_perspective(usages: list[dict[str, Any]], teams: list[dict[str, Any]], lang: str, scope: str = "overall") -> str:
    return regional_perspective(usages, teams, lang, scope)


def build_recent_squad_usage(fixture: dict[str, Any], lang: str = "en") -> dict[str, Any]:
    """Aggregate each side's most recent completed matches in the fixture season.

    Each team is intentionally sampled independently: ten completed matches if
    available, otherwise every completed match from that season.
    """
    season_id = _as_int((fixture.get("season") or {}).get("id"))
    season_name = str((fixture.get("season") or {}).get("name") or "")
    competition_id = _as_int((fixture.get("league") or {}).get("id"))
    reference_date = str(fixture.get("startingAt") or "")
    teams = [{**dict(fixture.get("homeTeam") or {}), "location": "home"}, {**dict(fixture.get("awayTeam") or {}), "location": "away"}]
    with ThreadPoolExecutor(max_workers=2) as executor:
        usages = list(
            executor.map(
                lambda team: _team_recent_squad_usage(team, season_id, season_name, competition_id, reference_date,
                                                     _include_finding_snapshots=True),
                teams,
            )
        )
    from .pre_match_competition_findings import VERSION as SCOPED_FINDINGS_VERSION, build_scoped_findings, plan_findings
    snapshots = {}
    for usage in usages:
        snapshots.update(usage.pop('_finding_snapshots', {}))
    from standings_module.comparison_ranks import enrich_comparison_ranks
    from .pre_match_player_comparison import comparison_player, enrich_player_comparison
    comparison = {'stats_scope': 'selected_matches', 'teams': [{'team_id': team['team_id'], 'sample_size': team.pop('comparison_sample_size', 0),
                            'selected_fixture_ids': team.pop('comparison_selected_fixture_ids', []), 'status': 'ready',
                            'players': [comparison_player(player) for player in team.pop('comparison_players', [])]}
                           for team in usages]}
    result = enrich_comparison_ranks({"teams": usages, 'player_comparison': comparison}, fixture)
    result = enrich_player_comparison(result, fixture)
    def publish(section):
        emit_progress({"recent_squad_usage": result}, section)
    publish("recent_statistics")
    result["head_to_head"] = _head_to_head_results(teams[0], teams[1], season_id, reference_date)
    publish("head_to_head")
    from .pre_match_findings import FINDINGS_VERSION, attach_tactical_plans, build_pre_match_findings, enrich_tactical_plans
    scoped = build_scoped_findings(usages, lang, fixture, snapshots)
    if len(scoped) < len(usages):
        scoped = {**build_scoped_findings(usages, lang, fixture), **scoped}
    result["scoped_team_findings"] = scoped
    result["scoped_team_findings_version"] = SCOPED_FINDINGS_VERSION if len(scoped) == len(usages) else 0
    defaults = plan_findings(scoped)
    combined = build_pre_match_findings(result["teams"], lang, teams) if len(defaults) < len(usages) else {}
    result["team_findings"] = {**combined, **defaults}
    result["team_findings_version"] = FINDINGS_VERSION if result["team_findings"] else 0
    publish("pre_team_analysis")
    result["regional_perspectives"] = {scope: _pre_match_regional_perspective(usages, teams, lang, scope) for scope in ("overall", "first_half", "second_half")}
    result["regional_perspective"] = result["regional_perspectives"]["overall"]
    publish("pre_regional")
    overview, plans = _pre_match_overview_and_plans(
        result["teams"], teams, result["team_findings"],
        result["regional_perspective"], lang, player_comparison=result.get("player_comparison"),
    )
    result["overview_summary"] = overview
    result.update(attach_tactical_plans(result, plans, teams, lang))
    if not result.get("team_tactical_plans"):
        result.update(enrich_tactical_plans(result, teams, lang))
    if not result.get("team_tactical_plans"):
        strategy = ("Rakibin alan bıraktığı anlarda top kontrollü biçimde son bölgeye taşınmalı."
                    if lang == "tr" else "Progress the ball into the final third when the opponent leaves space.")
        result["team_analysis"] = {str(team["id"]): {"strategy": strategy} for team in teams if team.get("id")}
    publish("pre_overview")
    return result



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
