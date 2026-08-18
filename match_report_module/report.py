from __future__ import annotations

import os
import json
import re
from collections import defaultdict
from typing import Any

import requests


SPORTMONKS_BASE_URL = os.getenv(
    "SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3/football"
).rstrip("/")
MATCH_REPORT_VERSION = 37

MATCH_REPORT_INCLUDE = ";".join(
    [
        "participants",
        "league.country",
        "season",
        "stage",
        "round",
        "venue",
        "state",
        "scores",
        "formations",
        "coaches",
        "referees",
        "periods.type",
        "periods.statistics.type",
        "statistics.type",
        "lineups.player",
        "lineups.position",
        "lineups.details.type",
        "events.type",
        "metadata",
        "weatherReport",
        "xGFixture.type",
        "lineups.xGLineup.type",
        "pressure",
        "trends",
        "ballCoordinates",
    ]
)

# These are the exact conceptual groups used by the player scouting report.
METRIC_GROUPS = (
    "contribution_impact",
    "goalkeeping",
    "shooting",
    "passing",
    "set_pieces",
    "defending",
    "errors_discipline",
)

TEAM_METRIC_CATEGORY: dict[str, tuple[str, str]] = {
    "Attacks": ("contribution_impact", "Attacks"),
    "Dangerous Attacks": ("contribution_impact", "Dangerous Attacks"),
    "Ball Possession %": ("contribution_impact", "Ball Possession %"),
    "Shots Total": ("shooting", "Shots Total"),
    "Shots On Target": ("shooting", "Shots On Target"),
    "Goals": ("shooting", "Goals"),
    "Expected Goals (xG)": ("shooting", "Expected Goals"),
    "Expected Goals on Target (xGoT)": ("shooting", "Expected Goals On Target"),
    "Shooting Performance (SP)": ("shooting", "Shooting Performance"),
    "Shots Insidebox": ("shooting", "Shots Insidebox"),
    "Shots Outsidebox": ("shooting", "Shots Outsidebox"),
    "Goal Attempts": ("shooting", "Goal Attempts"),
    "Passes": ("passing", "Passes"),
    "Successful Passes": ("passing", "Accurate Passes"),
    "Successful Passes Percentage": ("passing", "Accurate Passes (%)"),
    "Long Passes": ("passing", "Long Balls"),
    "Successful Long Passes": ("passing", "Long Balls Won"),
    "Successful Long Passes Percentage": ("passing", "Long Balls Won (%)"),
    "Total Crosses": ("passing", "Total Crosses"),
    "Accurate Crosses": ("passing", "Accurate Crosses"),
    "Key Passes": ("passing", "Key Passes"),
    "Corners": ("set_pieces", "Corners"),
    "Goal Kicks": ("set_pieces", "Goal Kicks"),
    "Free Kicks": ("set_pieces", "Free Kicks"),
    "Throwins": ("set_pieces", "Throwins"),
    "Penalties Scored": ("set_pieces", "Penalties Scored"),
    "Penalties Missed": ("set_pieces", "Penalties Missed"),
    "Penalties Won": ("set_pieces", "Penalties Won"),
    "Penalties Committed": ("set_pieces", "Penalties Committed"),
    "Penalties Saved": ("set_pieces", "Penalties Saved"),
    "Big Chances Created": ("contribution_impact", "Big Chances Created"),
    "Dribble Attempts": ("contribution_impact", "Dribble Attempts"),
    "Successful Dribbles": ("contribution_impact", "Successful Dribbles"),
    "Successful Dribbles Percentage": ("contribution_impact", "Dribble Accuracy (%)"),
    "Saves": ("defending", "Saves"),
    "Tackles": ("defending", "Tackles"),
    "Shots Blocked": ("defending", "Blocked Shots"),
    "Interceptions": ("defending", "Interceptions"),
    "Duels Won": ("defending", "Duels Won"),
    "Successful Headers": ("defending", "Successful Headers"),
    "Ball Safe": ("passing", "Ball Safe"),
    "Shots Off Target": ("errors_discipline", "Shots Off Target"),
    "Big Chances Missed": ("errors_discipline", "Big Chances Missed"),
    "Fouls": ("errors_discipline", "Fouls"),
    "Offsides": ("errors_discipline", "Offsides"),
    "Yellowcards": ("errors_discipline", "Yellow Cards"),
    "Redcards": ("errors_discipline", "Red Cards"),
}

PLAYER_METRIC_CATEGORY: dict[str, tuple[str, str]] = {
    **TEAM_METRIC_CATEGORY,
    "Minutes Played": ("contribution_impact", "Minutes Played"),
    "Touches": ("contribution_impact", "Touches"),
    "Rating": ("contribution_impact", "Rating"),
    "Captain": ("contribution_impact", "Captain"),
    "Fouls Drawn": ("contribution_impact", "Fouls Drawn"),
    "Assists": ("passing", "Assists"),
    "Accurate Passes": ("passing", "Accurate Passes"),
    "Accurate Passes Percentage": ("passing", "Accurate Passes (%)"),
    "Long Balls Won": ("passing", "Long Balls Won"),
    "Long Balls Won Percentage": ("passing", "Long Balls Won (%)"),
    "Passes In Final Third": ("passing", "Passes In Final Third"),
    "Backward Passes": ("passing", "Backward Passes"),
    "Long Balls": ("passing", "Long Balls"),
    "Successful Crosses Percentage": ("passing", "Successful Crosses Percentage"),
    "Chances Created": ("contribution_impact", "Chances Created"),
    "Blocked Shots": ("defending", "Blocked Shots"),
    "Saves Insidebox": ("goalkeeping", "Saves Insidebox"),
    "Punches": ("goalkeeping", "Punches"),
    "Good High Claim": ("goalkeeping", "Good High Claim"),
    "Tackles Won": ("defending", "Tackles Won"),
    "Tacles Won Percentage": ("defending", "Tackles Won (%)"),
    "Clearances": ("defending", "Clearances"),
    "Ball Recovery": ("defending", "Ball Recovery"),
    "Aerials": ("defending", "Aerials"),
    "Aerials Won": ("defending", "Aerials Won"),
    "Aerials Won Percentage": ("defending", "Aerials Won (%)"),
    "Total Duels": ("defending", "Total Duels"),
    "Duels Won Percentage": ("defending", "Duels Won (%)"),
    "Goals Conceded": ("errors_discipline", "Goals Conceded"),
    "Goalkeeper Goals Conceded": ("errors_discipline", "Goals Conceded"),
    "Aerials Lost": ("errors_discipline", "Aerials Lost"),
    "Duels Lost": ("errors_discipline", "Duels Lost"),
    "Dispossessed": ("errors_discipline", "Dispossessed"),
    "Dribbled Past": ("errors_discipline", "Dribbled Past"),
    "Possession Lost": ("errors_discipline", "Possession Lost"),
    "Error Lead To Goal": ("errors_discipline", "Error Lead To Goal"),
    "Error Lead To Shot": ("errors_discipline", "Error Lead To Shot"),
    "Yellowcards": ("errors_discipline", "Yellow Cards"),
    "Redcards": ("errors_discipline", "Red Cards"),
}


class MatchReportError(RuntimeError):
    pass


def _value(item: dict[str, Any]) -> Any:
    data = item.get("data") or {}
    return data.get("value") if isinstance(data, dict) else data


def _metric(name: str, value: Any, type_id: Any = None) -> dict[str, Any]:
    return {"name": name, "value": value, "type_id": type_id}


def _empty_categories() -> dict[str, list[dict[str, Any]]]:
    return {name: [] for name in METRIC_GROUPS}


def _categorize(
    rows: list[dict[str, Any]],
    mapping: dict[str, tuple[str, str]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    categories = _empty_categories()
    extras: list[dict[str, Any]] = []
    for row in rows:
        type_obj = row.get("type") or {}
        original_name = str(type_obj.get("name") or row.get("name") or row.get("type_id") or "")
        item = _metric(original_name, _value(row), row.get("type_id"))
        category = mapping.get(original_name)
        if category:
            group, canonical_name = category
            item["name"] = canonical_name
            item["source_name"] = original_name
            categories[group].append(item)
        else:
            extras.append(item)
    return categories, extras


def _numeric_metric(rows: list[dict[str, Any]], name: str) -> float | None:
    for row in rows:
        if row.get("name") != name:
            continue
        try:
            return float(row.get("value"))
        except (TypeError, ValueError):
            return None
    return None


def _upsert_percentage(
    rows: list[dict[str, Any]],
    name: str,
    numerator: float | None,
    denominator: float | None,
) -> None:
    if numerator is None or denominator is None or denominator <= 0:
        return
    rows[:] = [row for row in rows if row.get("name") != name]
    rows.append(_metric(name, round(numerator / denominator * 100, 1)))


def _derive_player_metrics(
    categories: dict[str, list[dict[str, Any]]],
    expected: list[dict[str, Any]],
) -> None:
    shooting = categories["shooting"]
    passing = categories["passing"]
    impact = categories["contribution_impact"]
    defending = categories["defending"]
    shots_total = _numeric_metric(shooting, "Shots Total")
    shots_on_target = _numeric_metric(shooting, "Shots On Target")
    goals = _numeric_metric(shooting, "Goals")
    expected_to_shooting = {
        "Expected Goals (xG)": "Expected Goals",
        "Expected Goals on Target (xGoT)": "Expected Goals On Target",
        "Shooting Performance (SP)": "Shooting Performance",
    }
    for expected_name, shooting_name in expected_to_shooting.items():
        value = _numeric_metric(expected, expected_name)
        if value is not None and _numeric_metric(shooting, shooting_name) is None:
            shooting.append(_metric(shooting_name, value))
    _upsert_percentage(shooting, "Shots On Target (%)", shots_on_target, shots_total)
    _upsert_percentage(shooting, "Goal Conversion (%)", goals, shots_total)
    _upsert_percentage(shooting, "On Target Goal Conversion (%)", goals, shots_on_target)
    _upsert_percentage(
        passing,
        "Assist Efficiency (%)",
        _numeric_metric(passing, "Assists"),
        _numeric_metric(passing, "Key Passes"),
    )
    _upsert_percentage(
        impact,
        "Dribble Accuracy (%)",
        _numeric_metric(impact, "Successful Dribbles"),
        _numeric_metric(impact, "Dribble Attempts"),
    )
    _upsert_percentage(
        defending,
        "Tackles Won (%)",
        _numeric_metric(defending, "Tackles Won"),
        _numeric_metric(defending, "Tackles"),
    )
    _upsert_percentage(
        defending,
        "Aerials Won (%)",
        _numeric_metric(defending, "Aerials Won"),
        _numeric_metric(defending, "Aerials"),
    )
    _upsert_percentage(
        defending,
        "Duels Won (%)",
        _numeric_metric(defending, "Duels Won"),
        _numeric_metric(defending, "Total Duels"),
    )
    _upsert_percentage(
        passing,
        "Long Balls Won (%)",
        _numeric_metric(passing, "Long Balls Won"),
        _numeric_metric(passing, "Long Balls"),
    )
    _upsert_percentage(
        passing,
        "Accurate Passes (%)",
        _numeric_metric(passing, "Accurate Passes"),
        _numeric_metric(passing, "Passes"),
    )
    _upsert_percentage(
        passing,
        "Accurate Crosses (%)",
        _numeric_metric(passing, "Accurate Crosses"),
        _numeric_metric(passing, "Total Crosses"),
    )
    _upsert_percentage(
        shooting,
        "Shot Quality (%)",
        _numeric_metric(expected, "Expected Goals (xG)"),
        shots_total,
    )
    _upsert_percentage(
        shooting,
        "On Target Shot Quality (%)",
        _numeric_metric(expected, "Expected Goals on Target (xGoT)"),
        shots_on_target,
    )


def _derive_team_percentages(categories: dict[str, list[dict[str, Any]]]) -> None:
    shooting = categories["shooting"]
    passing = categories["passing"]
    _upsert_percentage(
        shooting,
        "Shots On Target (%)",
        _numeric_metric(shooting, "Shots On Target"),
        _numeric_metric(shooting, "Shots Total"),
    )
    _upsert_percentage(
        passing,
        "Accurate Crosses (%)",
        _numeric_metric(passing, "Accurate Crosses"),
        _numeric_metric(passing, "Total Crosses"),
    )


def _team_stat_index(team: dict[str, Any]) -> dict[str, float]:
    index: dict[str, float] = {}
    for rows in team.get("categories", {}).values():
        for item in rows:
            try:
                index[str(item.get("name"))] = float(item.get("value"))
            except (TypeError, ValueError):
                continue
    for item in team.get("extra_metrics", []):
        try:
            index[str(item.get("name"))] = float(item.get("value"))
        except (TypeError, ValueError):
            continue
    return index


def _build_summary(teams: list[dict[str, Any]], events: list[dict[str, Any]], lang: str) -> list[str]:
    if len(teams) != 2:
        return []
    home = next((team for team in teams if team.get("location") == "home"), teams[0])
    away = next((team for team in teams if team.get("location") == "away"), teams[1])
    hi, ai = _team_stat_index(home), _team_stat_index(away)
    home_name, away_name = home.get("name"), away.get("name")
    if lang == "tr":
        points = [
            f"{home_name} toplam şutlarda {hi.get('Shots Total', 0):g}-{ai.get('Shots Total', 0):g} üstünlük kurdu.",
            f"Topa sahip olma dağılımı {home_name} %{hi.get('Ball Possession %', 0):g}, {away_name} %{ai.get('Ball Possession %', 0):g} olarak gerçekleşti.",
            f"Pas başarısı {home_name} için %{hi.get('Accurate Passes (%)', 0):g}, {away_name} için %{ai.get('Accurate Passes (%)', 0):g} oldu.",
        ]
        red_events = [event for event in events if str(event.get("type", "")).lower() == "redcard"]
        if red_events:
            points.append(f"Maçta {len(red_events)} kırmızı kart olayı kaydedildi ve oyun dengesi değişti.")
        return points
    points = [
        f"{home_name} led total shots {hi.get('Shots Total', 0):g}-{ai.get('Shots Total', 0):g}.",
        f"Possession finished at {hi.get('Ball Possession %', 0):g}% for {home_name} and {ai.get('Ball Possession %', 0):g}% for {away_name}.",
        f"Pass accuracy was {hi.get('Accurate Passes (%)', 0):g}% for {home_name} and {ai.get('Accurate Passes (%)', 0):g}% for {away_name}.",
    ]
    return points


def _build_scoutwise_perspective(report: dict[str, Any], lang: str) -> list[str]:
    teams = report.get("teams") or []
    events = report.get("events") or []
    pressure = report.get("pressure") or []
    compact = {
        "teams": [
            {
                "name": team.get("name"),
                "location": team.get("location"),
                "metrics": _team_stat_index(team),
            }
            for team in teams
        ],
        "events": [
            {
                "minute": event.get("minute"),
                "team": event.get("team_name"),
                "type": event.get("type"),
                "player": event.get("player_name"),
                "result": event.get("result"),
            }
            for event in events
        ],
        "pressure": {
            str(team.get("name")): [
                {"minute": row.get("minute"), "value": row.get("value")}
                for row in pressure
                if row.get("team_id") == team.get("id")
            ]
            for team in teams
        },
    }
    fallback = [str(item).strip() for item in (report.get("summary") or []) if str(item).strip()][:3]
    if not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.25,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's football match analyst. Return exactly two lines, each no longer than 55 words. Line 1 must chronologically interpret the first half (0-45+); line 2 must interpret the second half (46-full time), including the result. Never expose raw pressure-index numbers. Translate them into match-relative qualitative levels such as low, moderate, high, very high, rising, falling, or dominant, tied to concrete minute ranges. Relate goals, red cards, and substitutions to nearby pressure changes using observational wording such as 'coincided with', never proven causation. Do not add headings, bullets, markdown, caveats, or recommendations.",
                ),
                (
                    "human",
                    f"Language: {language}\nMatch data:\n{json.dumps(compact, ensure_ascii=False, default=str)}",
                ),
            ]
        )
        raw = str(response.content or "").strip()
        lines = [re.sub(r"^\s*[-•]\s*", "", line).strip() for line in raw.splitlines() if line.strip()]
        if len(lines) < 2:
            lines = [part.strip() for part in re.split(r"(?<=[.!?])\s+", raw) if part.strip()]
        return lines[:2] or fallback[:2]
    except Exception as exc:
        print(f"[enterprise_match_report] event=perspective_fallback error={exc}")
        return fallback


def _build_regional_play_perspective(report: dict[str, Any], lang: str) -> str:
    rows = report.get("ball_coordinates") or []
    counts = [0] * 9
    for row in rows:
        try:
            x = min(0.999999, max(0.0, float(row.get("x") or 0)))
            y = min(0.999999, max(0.0, float(row.get("y") or 0)))
            counts[int(y * 3) * 3 + int(x * 3)] += 1
        except (TypeError, ValueError):
            continue
    total = sum(counts) or 1
    zones = [round(count / total * 100, 1) for count in counts]
    valid_rows = []
    for row in rows:
        try:
            valid_rows.append((float(row.get("x") or 0), float(row.get("y") or 0)))
        except (TypeError, ValueError):
            continue
    valid_total = len(valid_rows) or 1
    semantic = {
        "wide_channels_z1_z2_z3_z7_z8_z9_pct": round(sum(zones[index] for index in (0, 1, 2, 6, 7, 8)), 1),
        "central_channel_z4_z5_z6_pct": round(sum(zones[index] for index in (3, 4, 5)), 1),
        "middle_field_band_z2_z5_z8_pct": round(sum(zones[index] for index in (1, 4, 7)), 1),
        "outer_end_bands_z1_z4_z7_and_z3_z6_z9_pct": round(sum(zones[index] for index in (0, 3, 6, 2, 5, 8)), 1),
        "penalty_area_vicinities_combined_pct": round(sum(1 for x, y in valid_rows if (x <= 16.5 / 105 or x >= 88.5 / 105) and 13.84 / 68 <= y <= 54.16 / 68) / valid_total * 100, 1),
    }
    fallback = (
        "Kaydedilen top konumları, sahanın belirli bölgelerinde daha yoğun bir dağılım gösterdi."
        if lang == "tr"
        else "Recorded ball locations showed a greater concentration in specific pitch zones."
    )
    if not rows or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.2,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's football match analyst. Write exactly one concise sentence, at most 45 words, interpreting the recorded ball-location pattern using these rules: concentration in Z2/Z5/Z8 means play stayed around the middle-field band; concentration in Z1/Z4/Z7 or Z3/Z6/Z9 means play reached the more dangerous areas nearer either end more often; concentration in Z1/Z2/Z3 and Z7/Z8/Z9 means the wide channels were used more; concentration in Z4/Z5/Z6 means the central channel was used more. Compare these patterns and describe the match's regional play structure and use of space. State only what the distribution positively indicates; never add a limitation, uncertainty, capability disclaimer, or a phrase such as 'does not prove', 'cannot determine', 'alone is insufficient', or an equivalent. Use natural football-analysis terminology such as 'alan kullanımı', 'oyunun merkezi', 'geniş alanlar', 'kanatlar', or their equivalents in the requested language. Never use 'coğrafya' or 'geography'. Prefer qualitative football language over numbers and zone IDs. Never mention K1, K2, goal names/labels, left or right goal, team possession, attacking direction, defensive thirds, or attacking thirds. Do not add headings, bullets, markdown, or recommendations.",
                ),
                (
                    "human",
                    f"Language: {language}\nZones in visual row-major order Z1-Z9 (top-left to bottom-right), percentages: {zones}\nSemantic spatial aggregates: {json.dumps(semantic, ensure_ascii=False)}",
                ),
            ]
        )
        value = re.sub(r"\s+", " ", str(response.content or "")).strip()
        return value or fallback
    except Exception as exc:
        print(f"[enterprise_match_report] event=regional_perspective_fallback error={exc}")
        return fallback


def _build_team_analysis_perspectives(
    teams: list[dict[str, Any]],
    period_teams: dict[str, list[dict[str, Any]]],
    lang: str,
) -> dict[str, str]:
    scopes = {"overall": teams, **period_teams}
    compact: dict[str, Any] = {}
    for scope, scope_teams in scopes.items():
        compact[scope] = {}
        for team in scope_teams:
            team_values = {
                group: {item.get("name"): item.get("value") for item in rows}
                for group, rows in (team.get("categories") or {}).items()
                if rows and group != "goalkeeping"
            }
            if scope == "overall" and team.get("expected_metrics"):
                team_values["expected"] = {
                    item.get("name"): item.get("value")
                    for item in team.get("expected_metrics") or []
                }
            compact[scope][team.get("name")] = team_values
    fallback_label = "Genel ve yarı bazındaki değerler, iki takımın bu alandaki performans farklarını gösterir."
    fallback = {
        group: fallback_label
        for group in (
            "contribution_impact", "shooting", "passing", "set_pieces",
            "defending", "errors_discipline", "expected",
        )
    }
    if not os.getenv("OPENAI_API_KEY"):
        return fallback


    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.2,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's senior football match analyst. Return only one valid JSON object keyed by category, never by scope. For every available category, write one stable, concise, evidence-led analysis of 50-75 words in the requested language. Use only the most diagnostic statistics explicitly and explain their meaning rather than producing a list. Compare overall, first-half, and second-half values together, retaining only the clearest change after halftime, volume-versus-efficiency gap, or contradiction between related metrics. Prefer 2-3 direct sentences with no repetition. The same analysis is displayed in all period switch states. Never invent causation, tactical intent, attack direction, or unavailable facts; phrase relationships as evidence or indications, not certainty. Do not use markdown or headings.",
                ),
                (
                    "human",
                    f"Language: {language}\nScope/category team metrics:\n{json.dumps(compact, ensure_ascii=False, default=str)}",
                ),
            ]
        )
        raw = str(response.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
        parsed = json.loads(raw)
        available_groups = {
            group
            for scope_data in compact.values()
            for team_data in scope_data.values()
            for group in team_data
        }
        result: dict[str, str] = {}
        for group in available_groups:
            value = str(parsed.get(group) or "").strip()
            result[group] = value or fallback[group]
        return result
    except Exception as exc:
        print(f"[enterprise_match_report] event=team_perspective_fallback error={exc}")
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.2,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's football match analyst. Write exactly one concise sentence, at most 45 words, interpreting the recorded ball-location pattern using these rules: concentration in Z2/Z5/Z8 means play stayed around the middle-field band; concentration in Z1/Z4/Z7 or Z3/Z6/Z9 means play reached the more dangerous areas nearer either end more often; concentration in Z1/Z2/Z3 and Z7/Z8/Z9 means the wide channels were used more; concentration in Z4/Z5/Z6 means the central channel was used more. Compare these patterns and describe the match's regional play structure and use of space. Use natural football-analysis terminology such as 'alan kullanımı', 'oyunun merkezi', 'geniş alanlar', 'kanatlar', or their equivalents in the requested language. Never use 'coğrafya' or 'geography'. Prefer qualitative football language over numbers and zone IDs. Never mention K1, K2, goal names/labels, left or right goal, team possession, attacking direction, defensive thirds, or attacking thirds. Do not add headings, bullets, markdown, or recommendations.",
                ),
                (
                    "human",
                    f"Language: {language}\nZones in visual row-major order Z1-Z9 (top-left to bottom-right), percentages: {zones}\nSemantic spatial aggregates: {json.dumps(semantic, ensure_ascii=False)}",
                ),
            ]
        )
        value = re.sub(r"\s+", " ", str(response.content or "")).strip()
        return value or fallback
    except Exception as exc:
        print(f"[enterprise_match_report] event=regional_perspective_fallback error={exc}")
        return fallback


def _build_player_analysis_perspectives(
    teams: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
    events: list[dict[str, Any]],
    lang: str,
) -> dict[str, list[dict[str, Any]]]:
    def find_metric(player: dict[str, Any], name: str) -> float | None:
        for rows in (player.get("categories") or {}).values():
            for metric in rows or []:
                if metric.get("name") == name:
                    try:
                        return float(metric.get("value"))
                    except (TypeError, ValueError):
                        return None
        return None

    selected: dict[str, list[dict[str, Any]]] = {}
    compact: dict[str, Any] = {}
    for team in teams:
        candidates = []
        for player in lineups:
            if player.get("team_id") != team.get("id"):
                continue
            minutes = find_metric(player, "Minutes Played") or 0
            rating = find_metric(player, "Rating")
            if minutes > 0 and rating is not None:
                player_events = [event for event in events if event.get("player_id") == player.get("player_id")]
                goals = sum(1 for event in player_events if str(event.get("type") or "").lower() == "goal")
                red_card = any("red" in str(event.get("type") or "").lower() for event in player_events)
                candidates.append({
                    "rating": rating,
                    "minutes": minutes,
                    "goals": goals,
                    "red_card": red_card,
                    "player": player,
                })
        rating_order = sorted(
            candidates,
            key=lambda item: (-item["rating"], -item["minutes"], str(item["player"].get("player_name") or "")),
        )
        scoring_order = sorted(
            [item for item in candidates if item["goals"] > 0],
            key=lambda item: (-item["goals"], -item["rating"], -item["minutes"]),
        )
        featured: list[dict[str, Any]] = []
        top_scorer = scoring_order[0] if scoring_order else None
        if top_scorer and not top_scorer["red_card"]:
            featured.append(top_scorer)
        for item in rating_order:
            if all(item["player"].get("player_id") != chosen["player"].get("player_id") for chosen in featured):
                featured.append(item)
            if len(featured) == 2:
                break
        featured_ids = {item["player"].get("player_id") for item in featured}
        worst_pool = [
            item for item in candidates
            if item["minutes"] > 30 and item["player"].get("player_id") not in featured_ids
        ]
        worst = min(
            worst_pool,
            key=lambda item: (item["rating"], -item["minutes"], str(item["player"].get("player_name") or "")),
            default=None,
        )
        chosen_players = [(item["player"], "featured") for item in featured]
        if worst:
            chosen_players.append((worst["player"], "development"))
        team_key = str(team.get("id"))
        compact_players = []
        selected[team_key] = []
        for player, selection_type in chosen_players:
            selected[team_key].append({
                "player_id": player.get("player_id"),
                "player_name": player.get("player_name"),
                "selection_type": selection_type,
                "text": "",
            })
            compact_players.append({
                "player_id": player.get("player_id"),
                "player_name": player.get("player_name"),
                "selection_type": selection_type,
                "position": player.get("position_name"),
                "formation_field": player.get("formation_field"),
                "formation_position": player.get("formation_position"),
                "starter": player.get("starter"),
                "metrics": {
                    group: {metric.get("name"): metric.get("value") for metric in rows or []}
                    for group, rows in (player.get("categories") or {}).items() if rows
                },
                "expected_metrics": {
                    metric.get("name"): metric.get("value")
                    for metric in player.get("expected_metrics") or []
                },
                "events": [
                    {key: event.get(key) for key in ("type", "minute", "extra_minute", "info", "addition")}
                    for event in events
                    if event.get("player_id") == player.get("player_id")
                    or event.get("related_player_id") == player.get("player_id")
                ],
            })
        compact[team_key] = {"team_name": team.get("name"), "players": compact_players}

    for team_key, rows in selected.items():
        for index, row in enumerate(rows):
            player_data = compact[team_key]["players"][index]
            contribution = player_data["metrics"].get("contribution_impact", {})
            rating = contribution.get("Rating", "—")
            minutes = contribution.get("Minutes Played", "—")
            if row.get("selection_type") == "development":
                row["text"] = (
                    f"{row['player_name']}, {rating} rating ile {minutes} dakikalık performansında takımının daha sınırlı kalan isimlerinden biri oldu. Mevki sorumlulukları içinde düşük kalan üretim ve hata göstergeleri, gelişim alanının temelini oluşturdu."
                    if lang == "tr"
                    else f"{row['player_name']} recorded a {rating} rating across {minutes} minutes and produced one of the team's more limited performances. Lower output and error indicators within the positional role define the main development area."
                )
            else:
                row["text"] = (
                    f"{row['player_name']}, {rating} rating ile {minutes} dakika boyunca takımının öne çıkan performanslarından birini verdi. Mevki rolündeki yüksek katkı değerleri ve maç içindeki etkili aksiyonları bu seçimi destekledi."
                    if lang == "tr"
                    else f"{row['player_name']} delivered one of the team's standout displays with a {rating} rating across {minutes} minutes. Strong contribution values and influential actions within the positional role support the selection."
                )
    if not any(selected.values()) or not os.getenv("OPENAI_API_KEY"):
        return selected
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.2).invoke([
            (
                "system",
                "You are ScoutWise Enterprise's senior player-performance analyst. Return only valid JSON keyed by the supplied team IDs. Each value must preserve the supplied three-player order and contain exactly player_id and text. Write one focused, evidence-led interpretation of 45-65 words per player in the requested language, considering the player's position. Begin naturally with the player's name and the central meaning of the performance; never open with formulaic constructions such as 'a centre-back who played 71 minutes', 'playing 90 minutes as a midfielder', or their equivalents. Minutes and position may appear later only when analytically useful. For selection_type=featured, write an exclusively positive assessment: emphasize the player's highest and most influential metric values, scoring or creative output, efficiency, rating, position-specific strengths, and positive match impact. Do not include any negative sentence, limitation, weakness, loss, error, missed chance, low efficiency, adverse contrast, or a transition such as 'however'. For selection_type=development, focus constructively on errors and discipline, low values, inefficiency, lost possessions or duels, missed opportunities, and position-specific shortcomings, while acknowledging positive evidence only as context. Combine rating, minutes, role, events, volume and efficiency where they support the assigned selection type. Never invent actions, tactics, causation, or metrics. Use clear sentences with no headings, markdown, bullets, recommendations, or raw category names.",
            ),
            ("human", f"Language: {language}\nSelected standout and development-area players with match data:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        for team_key, rows in selected.items():
            generated = parsed.get(team_key) or []
            by_id = {str(item.get("player_id")): str(item.get("text") or "").strip() for item in generated}
            for row in rows:
                row["text"] = by_id.get(str(row.get("player_id"))) or row["text"]
        return selected
    except Exception as exc:
        print(f"[enterprise_match_report] event=player_perspective_fallback error={exc}")
        return selected


def _build_team_deep_analyses(report: dict[str, Any], lang: str) -> dict[str, list[dict[str, str]]]:
    teams = report.get("teams") or []
    period_teams = report.get("period_teams") or {}
    lineups = report.get("lineups") or []
    events = report.get("events") or []
    pressure = report.get("pressure") or []

    def metric_map(team: dict[str, Any]) -> dict[str, Any]:
        values = {
            group: {item.get("name"): item.get("value") for item in rows or []}
            for group, rows in (team.get("categories") or {}).items()
            if rows
        }
        if team.get("expected_metrics"):
            values["expected"] = {
                item.get("name"): item.get("value")
                for item in team.get("expected_metrics") or []
            }
        return values

    compact: dict[str, Any] = {"match": report.get("fixture"), "teams": {}}
    fallback_headers_tr = ["Oyun Kimliği", "Üretim ve Verimlilik", "Kadro Kullanımı", "Maçın Kırılma Anları", "Baskı ve Dayanıklılık"]
    fallback_headers_en = ["Game Identity", "Production and Efficiency", "Squad Usage", "Turning Points", "Pressure and Resilience"]
    fallback: dict[str, list[dict[str, str]]] = {}
    for team in teams:
        team_id = team.get("id")
        team_key = str(team_id)
        team_pressure = [row for row in pressure if row.get("team_id") == team_id]
        pressure_by_half: dict[str, Any] = {}
        for half, start, end in (("first_half", 0, 45), ("second_half", 46, 130)):
            rows = [row for row in team_pressure if start <= int(row.get("minute") or 0) <= end]
            values = [float(row.get("value") or 0) for row in rows]
            pressure_by_half[half] = {
                "average": round(sum(values) / len(values), 2) if values else 0,
                "maximum": round(max(values), 2) if values else 0,
                "peak_minutes": [
                    row.get("minute") for row in sorted(rows, key=lambda item: float(item.get("value") or 0), reverse=True)[:3]
                ],
            }
        team_lineups = []
        for player in lineups:
            if player.get("team_id") != team_id:
                continue
            contribution = {
                item.get("name"): item.get("value")
                for item in (player.get("categories") or {}).get("contribution_impact", [])
            }
            try:
                minutes = float(contribution.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            team_lineups.append({
                "player_id": player.get("player_id"),
                "name": player.get("player_name"),
                "position": player.get("position_name"),
                "starter": player.get("starter"),
                "minutes": minutes,
                "rating": contribution.get("Rating"),
                "metrics": metric_map(player),
            })
        scoped_periods = {}
        for scope, scoped_teams in period_teams.items():
            scoped_team = next((item for item in scoped_teams if item.get("id") == team_id), None)
            scoped_periods[scope] = metric_map(scoped_team or {})
        compact["teams"][team_key] = {
            "team_name": team.get("name"),
            "opponent": next((item.get("name") for item in teams if item.get("id") != team_id), None),
            "formation": team.get("formation"),
            "overall_metrics": metric_map(team),
            "period_metrics": scoped_periods,
            "players_used": team_lineups,
            "events": [event for event in events if event.get("team_id") == team_id],
            "pressure_summary": pressure_by_half,
        }
        headers = fallback_headers_tr if lang == "tr" else fallback_headers_en
        fallback[team_key] = [
            {"header": header, "text": ("Bu alan için maç verileri yeniden değerlendiriliyor." if lang == "tr" else "Match data is being reassessed for this area."), "tone": "neutral"}
            for header in headers
        ]
    if not teams or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.25).invoke([
            (
                "system",
                "You are ScoutWise Enterprise's senior match analyst. Return only valid JSON keyed by the supplied team IDs. Each team value must be an array of exactly five objects with exactly header, text, and tone. tone must be exactly positive, negative, or neutral. Assign positive when the central conclusion is a strength, successful effect, superiority, or effective response; negative when it is a weakness, inefficiency, error pattern, vulnerability, deterioration, or failed conversion; neutral when it is genuinely balanced, descriptive, or mixed without one dominant direction. Classify the central conclusion, not isolated sentences. Select five distinct, dynamic headers from the evidence; do not reuse a fixed template and do not use generic headings such as 'General Analysis'. Each header must capture the central football insight of its bullet in 2-5 words. Never mention, spell out, or encode a formation or numeric shape in a header: forbidden examples include '4-2-3-1', 'dört-iki-üç-bir', 'daralan 4-1-4-1', or 'üretken dört-iki-üç-bir'. Headers must describe the actual insight, such as control, width, finishing, resistance, transition risk, or late pressure. Formations may appear factually inside the text only. Each text must be a concise but evidence-led analysis of 40-60 words in the requested language. Read each team in relation to its opponent by combining formation, starters and substitutes with positions and formation fields, substitution timing, player ratings and metrics, overall and half-by-half team metrics, match events, score flow, and pressure/momentum summaries. Explain structural meaning, changes across the match, volume versus efficiency, personnel effects, strengths, vulnerabilities, and observable turning points without repeating evidence or packing several ideas into one sentence. Across the five bullets, cover shape/personnel, attacking production, possession/passing or progression, defensive/error discipline, and temporal pressure/event flow, but let the actual headers emerge from the match evidence. Formation fields establish only a player's line and slot, not a specific tactical role. Never label players as a number 6, 8, 10, regista, double pivot, double six, inverted fullback, or another inferred role unless that role is explicitly present in the supplied data. Prefer factual phrases such as 'central-midfield pair', 'same midfield line', or their natural equivalent. Do not claim causation from coincidence, attack direction, or unavailable tactical intent. Do not invent data. Do not use markdown, bullet characters, recommendations, or raw pressure numbers; translate pressure values into relative qualitative language.",
            ),
            ("human", f"Language: {language}\nFull match evidence by team:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        result: dict[str, list[dict[str, str]]] = {}
        for team_key in compact["teams"]:
            rows = parsed.get(team_key) or []
            cleaned = [
                {
                    "header": str(row.get("header") or "").strip(),
                    "text": str(row.get("text") or "").strip(),
                    "tone": str(row.get("tone") or "neutral").strip().lower()
                    if str(row.get("tone") or "").strip().lower() in {"positive", "negative", "neutral"}
                    else "neutral",
                }
                for row in rows[:5]
                if str(row.get("header") or "").strip() and str(row.get("text") or "").strip()
            ]
            if len(cleaned) == 5:
                tone_order = {"positive": 0, "neutral": 1, "negative": 2}
                result[team_key] = sorted(
                    cleaned,
                    key=lambda item: tone_order.get(item.get("tone", "neutral"), 1),
                )
            else:
                result[team_key] = fallback[team_key]
        return result
    except Exception as exc:
        print(f"[enterprise_match_report] event=team_deep_analysis_fallback error={exc}")
        return fallback


def _build_report_overview_summary(report: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    teams = report.get("teams") or []
    compact = {
        "fixture": report.get("fixture"),
        "league": report.get("league"),
        "venue": report.get("venue"),
        "state": report.get("state"),
        "teams": [
            {
                "id": team.get("id"),
                "name": team.get("name"),
                "location": team.get("location"),
                "formation": team.get("formation"),
            }
            for team in teams
        ],
        "score_flow": report.get("events"),
        "momentum_interpretation": report.get("scoutwise_perspective_points"),
        "regional_play_interpretation": report.get("regional_play_perspective"),
        "team_comparison_interpretations": report.get("team_analysis_perspectives"),
        "team_deep_analyses": report.get("team_deep_analyses"),
        "player_analysis_interpretations": report.get("player_analysis_perspectives"),
        "lineups": [
            {
                "player_name": player.get("player_name"),
                "team_name": player.get("team_name"),
                "position": player.get("position_name"),
                "starter": player.get("starter"),
                "formation_field": player.get("formation_field"),
                "rating": next(
                    (
                        metric.get("value")
                        for metric in (player.get("categories") or {}).get("contribution_impact", [])
                        if metric.get("name") == "Rating"
                    ),
                    None,
                ),
                "minutes": next(
                    (
                        metric.get("value")
                        for metric in (player.get("categories") or {}).get("contribution_impact", [])
                        if metric.get("name") == "Minutes Played"
                    ),
                    None,
                ),
            }
            for player in report.get("lineups") or []
        ],
    }
    categories_tr = ["Maç Kartı", "Kadro ve Diziliş", "Maç Akışı", "Momentum", "Bölgesel Oyun Dağılımı", "Takım Karşılaştırması", "Takım Analizi", "Oyuncu Analizi"]
    categories_en = ["Match Card", "Lineup & Formation", "Timeline", "Momentum", "Regional Play Distribution", "Team Comparison", "Team Analysis", "Player Analysis"]
    categories = categories_tr if lang == "tr" else categories_en
    fallback = [
        {"category": category, "summary": ("Bu bölümün rapor özeti hazırlanıyor." if lang == "tr" else "The report summary for this section is being prepared."), "sub_bullets": []}
        for category in categories
    ]
    if not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.2).invoke([
            (
                "system",
                "You are ScoutWise Enterprise's lead football-report editor. Return only a valid JSON array of exactly eight objects, in this exact report-section order: Match Card, Lineup & Formation, Timeline, Momentum, Regional Play Distribution, Team Comparison, Team Analysis, Player Analysis. Translate category names naturally into the requested language. Every object must contain exactly category, summary, and sub_bullets. Keep the same eight sections and be highly concise. summary must be a cohesive, evidence-led synthesis of 20-30 words containing only the section's decisive interpretation. Every sub-bullet object must contain exactly label and text, with text limited to 10-18 words. Apply these counts strictly: Match Card, Timeline, and Regional Play Distribution have zero sub-bullets; Lineup & Formation and Momentum have exactly two; Team Comparison, Team Analysis, and Player Analysis have exactly two. For Team Comparison and Team Analysis, create one sub-bullet per team and use that team's exact name as the label. For Player Analysis, select exactly one interpreted player from each team and use the player's full name as the label; never select two players from the same team. Integrate and compress the supplied existing ScoutWise interpretations; do not contradict them or introduce a new tactical claim. State only supported interpretations and what the evidence positively indicates. Never add defensive capability caveats such as 'does not prove', 'cannot determine', 'cannot establish', 'alone is insufficient', 'the data does not show', or their equivalents. In Regional Play Distribution especially, describe the supported spatial pattern, channel use, central versus wide concentration, and proximity to dangerous end areas without explaining what cannot be inferred. Preserve distinctions between observation, correlation, and causation through careful affirmative wording, not disclaimer sentences. Never invent data, attack direction, roles, or events. Avoid repeating the same fact across sections. Use polished, direct football-analysis language without markdown, bullet characters, recommendations, or generic filler.",
            ),
            ("human", f"Language: {language}\nReport evidence and existing interpretations:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) != 8:
            return fallback
        cleaned = []
        for index, row in enumerate(parsed):
            summary = str(row.get("summary") or "").strip()
            sub_bullets = [
                {"label": str(item.get("label") or "").strip(), "text": str(item.get("text") or "").strip()}
                for item in (row.get("sub_bullets") or [])[:3]
                if str(item.get("label") or "").strip() and str(item.get("text") or "").strip()
            ]
            cleaned.append({
                "category": categories[index],
                "summary": summary or fallback[index]["summary"],
                "sub_bullets": sub_bullets,
            })
        return cleaned
    except Exception as exc:
        print(f"[enterprise_match_report] event=overview_summary_fallback error={exc}")
        return fallback


def generate_match_report(fixture_id: int, lang: str = "en") -> dict[str, Any]:
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise MatchReportError("SPORTMONKS_API_KEY is not configured")
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/fixtures/{fixture_id}",
        params={"api_token": token, "include": MATCH_REPORT_INCLUDE},
        timeout=75,
    )
    if response.status_code != 200:
        try:
            message = response.json().get("message")
        except ValueError:
            message = None
        raise MatchReportError(f"{message or 'SportMonks match report request failed'} (HTTP {response.status_code})")
    fixture = response.json().get("data") or {}
    if not fixture:
        raise MatchReportError("SportMonks returned an empty fixture")

    participants = fixture.get("participants") or []
    team_by_id = {team.get("id"): team for team in participants}
    statistic_rows: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in fixture.get("statistics") or []:
        statistic_rows[row.get("participant_id")].append(row)
    expected_rows: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in fixture.get("xgfixture") or []:
        expected_rows[row.get("participant_id")].append(row)

    teams: list[dict[str, Any]] = []
    for team in participants:
        categories, extras = _categorize(statistic_rows.get(team.get("id"), []), TEAM_METRIC_CATEGORY)
        shooting_values = {
            str(metric.get("name")): metric.get("value")
            for metric in categories.get("shooting", [])
        }
        try:
            total_shots = float(shooting_values.get("Shots Total") or 0)
            shots_on_target = float(shooting_values.get("Shots On Target") or 0)
            if total_shots > 0:
                categories["shooting"].append(
                    _metric(
                        "Shots On Target (%)",
                        round(shots_on_target / total_shots * 100, 1),
                    )
                )
        except (TypeError, ValueError):
            pass
        passing_values = {
            str(metric.get("name")): metric.get("value")
            for metric in categories.get("passing", [])
        }
        try:
            total_crosses = float(passing_values.get("Total Crosses") or 0)
            accurate_crosses = float(passing_values.get("Accurate Crosses") or 0)
            if total_crosses > 0:
                categories["passing"].append(
                    _metric(
                        "Accurate Crosses (%)",
                        round(accurate_crosses / total_crosses * 100, 1),
                    )
                )
        except (TypeError, ValueError):
            pass
        expected = [
            _metric(str((row.get("type") or {}).get("name") or row.get("type_id")), _value(row), row.get("type_id"))
            for row in expected_rows.get(team.get("id"), [])
            if str((row.get("type") or {}).get("name") or "") != "Expected Goals Against (xGA)"
        ]
        teams.append(
            {
                "id": team.get("id"),
                "name": team.get("name"),
                "short_code": team.get("short_code"),
                "image_url": team.get("image_path"),
                "location": (team.get("meta") or {}).get("location"),
                "winner": (team.get("meta") or {}).get("winner"),
                "position": (team.get("meta") or {}).get("position"),
                "categories": categories,
                "extra_metrics": extras,
                "expected_metrics": expected,
            }
        )

    period_teams: dict[str, list[dict[str, Any]]] = {}
    for period in fixture.get("periods") or []:
        period_key = (
            "first_half"
            if period.get("type_id") == 1
            else "second_half"
            if period.get("type_id") == 2
            else None
        )
        if not period_key:
            continue
        rows_by_team: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in period.get("statistics") or []:
            rows_by_team[row.get("participant_id")].append(row)
        scoped_teams: list[dict[str, Any]] = []
        for full_team in teams:
            categories, extras = _categorize(
                rows_by_team.get(full_team.get("id"), []),
                TEAM_METRIC_CATEGORY,
            )
            _derive_team_percentages(categories)
            scoped_teams.append(
                {
                    **{key: full_team.get(key) for key in (
                        "id", "name", "short_code", "image_url", "location",
                        "winner", "position",
                    )},
                    "categories": categories,
                    "extra_metrics": extras,
                    "expected_metrics": [],
                }
            )
        period_teams[period_key] = scoped_teams

    formations = fixture.get("formations") or []
    formation_by_team = {row.get("participant_id"): row.get("formation") for row in formations}
    lineups: list[dict[str, Any]] = []
    player_expected_rows = 0
    for row in fixture.get("lineups") or []:
        categories, extras = _categorize(row.get("details") or [], PLAYER_METRIC_CATEGORY)
        expected = [
            _metric(str((item.get("type") or {}).get("name") or item.get("type_id")), _value(item), item.get("type_id"))
            for item in row.get("xglineup") or []
        ]
        _derive_player_metrics(categories, expected)
        if expected:
            player_expected_rows += 1
        lineups.append(
            {
                "id": row.get("id"),
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "player_image_url": (row.get("player") or {}).get("image_path"),
                "team_id": row.get("team_id"),
                "team_name": (team_by_id.get(row.get("team_id")) or {}).get("name"),
                "position_id": row.get("position_id"),
                "position_name": (row.get("position") or {}).get("name"),
                "type_id": row.get("type_id"),
                "starter": row.get("type_id") == 11,
                "jersey_number": row.get("jersey_number"),
                "formation_field": row.get("formation_field"),
                "formation_position": row.get("formation_position"),
                "categories": categories,
                "extra_metrics": extras,
                "expected_metrics": expected,
            }
        )

    events = []
    player_team = {row.get("player_id"): row.get("team_id") for row in fixture.get("lineups") or []}
    for row in fixture.get("events") or []:
        team_id = row.get("team_id") or player_team.get(row.get("player_id"))
        events.append(
            {
                "id": row.get("id"),
                "team_id": team_id,
                "team_name": (team_by_id.get(team_id) or {}).get("name"),
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "related_player_id": row.get("related_player_id"),
                "related_player_name": row.get("related_player_name"),
                "minute": row.get("minute"),
                "extra_minute": row.get("extra_minute"),
                "result": row.get("result"),
                "info": row.get("info"),
                "addition": row.get("addition"),
                "type_id": row.get("type_id"),
                "type": (row.get("type") or {}).get("name"),
            }
        )

    pressure = [
        {
            "team_id": row.get("participant_id"),
            "team_name": (team_by_id.get(row.get("participant_id")) or {}).get("name"),
            "minute": row.get("minute"),
            "value": row.get("pressure"),
        }
        for row in fixture.get("pressure") or []
    ]
    trends = [
        {
            "team_id": row.get("participant_id"),
            "team_name": (team_by_id.get(row.get("participant_id")) or {}).get("name"),
            "type_id": row.get("type_id"),
            "period_id": row.get("period_id"),
            "minute": row.get("minute"),
            "value": row.get("value"),
        }
        for row in fixture.get("trends") or []
    ]
    ball_coordinates = [
        {key: row.get(key) for key in ("period_id", "timer", "x", "y")}
        for row in fixture.get("ballcoordinates") or []
    ]

    report = {
        "language": lang,
        "version": MATCH_REPORT_VERSION,
        "fixture": {
            key: fixture.get(key)
            for key in (
                "id", "name", "starting_at", "starting_at_timestamp", "result_info",
                "length", "leg", "league_id", "season_id", "stage_id", "round_id",
                "venue_id", "state_id",
            )
        },
        "league": fixture.get("league"),
        "season": fixture.get("season"),
        "stage": fixture.get("stage"),
        "round": fixture.get("round"),
        "venue": fixture.get("venue"),
        "state": fixture.get("state"),
        "weather": fixture.get("weatherreport"),
        "coaches": fixture.get("coaches") or [],
        "referees": fixture.get("referees") or [],
        "scores": fixture.get("scores") or [],
        "periods": fixture.get("periods") or [],
        "formations": [
            {
                **row,
                "team_name": (team_by_id.get(row.get("participant_id")) or {}).get("name"),
            }
            for row in formations
        ],
        "teams": teams,
        "period_teams": period_teams,
        "lineups": lineups,
        "events": events,
        "pressure": pressure,
        "trends": trends,
        "ball_coordinates": ball_coordinates,
        "summary": _build_summary(teams, events, lang),
        "coverage": {
            "team_stat_rows": len(fixture.get("statistics") or []),
            "unique_team_metrics": len({
                metric.get("name")
                for team in teams
                for metric in (
                    [item for rows in team.get("categories", {}).values() for item in rows]
                    + team.get("expected_metrics", [])
                    + team.get("extra_metrics", [])
                )
                if metric.get("name")
            }),
            "player_stat_rows": sum(
                len(row.get("details") or []) + len(row.get("xglineup") or [])
                for row in fixture.get("lineups") or []
            ),
            "lineup_rows": len(lineups),
            "unique_player_metrics": len({
                metric.get("name")
                for player in lineups
                for metric in (
                    [item for rows in player.get("categories", {}).values() for item in rows]
                    + player.get("expected_metrics", [])
                    + player.get("extra_metrics", [])
                )
                if metric.get("name")
            }),
            "players_with_minutes": sum(
                1
                for player in lineups
                if any(
                    metric.get("name") == "Minutes Played"
                    and float(metric.get("value") or 0) > 0
                    for metric in player.get("categories", {}).get("contribution_impact", [])
                )
            ),
            "players_with_expected_metrics": player_expected_rows,
            "event_rows": len(events),
            "pressure_rows": len(pressure),
            "trend_rows": len(trends),
            "ball_coordinate_rows": len(ball_coordinates),
        },
        "metric_groups": list(METRIC_GROUPS),
        "temporary_extra_team_metrics": sorted(
            {item["name"] for team in teams for item in team.get("extra_metrics", [])}
        ),
    }
    for team in report["teams"]:
        team["formation"] = formation_by_team.get(team.get("id"))
    report["scoutwise_perspective_points"] = _build_scoutwise_perspective(report, lang)
    report["scoutwise_perspective"] = " ".join(report["scoutwise_perspective_points"])
    report["regional_play_perspective"] = _build_regional_play_perspective(report, lang)
    report["team_analysis_perspectives"] = _build_team_analysis_perspectives(
        teams,
        period_teams,
        lang,
    )
    report["player_analysis_perspectives"] = _build_player_analysis_perspectives(
        teams,
        lineups,
        events,
        lang,
    )
    report["team_deep_analyses"] = _build_team_deep_analyses(report, lang)
    report["overview_summary"] = _build_report_overview_summary(report, lang)
    report["analysis_model"] = os.getenv(
        "OPENAI_MATCH_REPORT_MODEL",
        os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
    )
    return report
