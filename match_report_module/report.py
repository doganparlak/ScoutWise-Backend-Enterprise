from __future__ import annotations

from report_module.penalty_xg import PENALTY_XG, penalty_xg_sample, penalty_xg_average
from report_module.progress import emit_progress
from report_module.terminology import FOOTBALL_TERMINOLOGY_GUIDANCE
from report_module.profile_evidence import PROFILE_EVIDENCE_GUIDANCE, build_evidence_profile
from report_module.profile_generation import generate_profile_json

import os
import json
import re
from collections import Counter, defaultdict
from typing import Any

import requests

from report_module.crossing_metrics import CROSSING_RATE, normalize_crossing_rows

from report_module.rate_evidence import missing_attempt_names, key as rate_key
from report_module.metric_validation import is_percentage, is_standard_count, zero_fill_counts, nonzero_evidence, percentage, rate_counts, pooled_rate_counts, valid_metric


SPORTMONKS_BASE_URL = os.getenv(
    "SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3/football"
).rstrip("/")
MATCH_REPORT_VERSION = 52
TEAM_ANALYSIS_REPORT_VERSION = 72

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
    "Successful Crosses Percentage": ("passing", "Accurate Crosses (%)"),
    "Successful Crosses (%)": ("passing", "Accurate Crosses (%)"),
    "Accurate Crosses (%)": ("passing", "Accurate Crosses (%)"),
    "Key Passes": ("passing", "Key Passes"),
    "Penalties": ("set_pieces", "Penalties"),
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

# Percentages that have a known numerator and denominator must be derived from
# their aggregated components, never averaged as independently rounded match
# percentages.
DERIVED_PERCENTAGE_METRICS: dict[str, tuple[str, str]] = {
    "Shots On Target (%)": ("Shots On Target", "Shots Total"),
    "Goal Conversion (%)": ("Goals", "Shots Total"),
    "On Target Goal Conversion (%)": ("Goals", "Shots On Target"),
    "Assist Efficiency (%)": ("Assists", "Key Passes"),
    "Dribble Accuracy (%)": ("Successful Dribbles", "Dribble Attempts"),
    "Tackles Won (%)": ("Tackles Won", "Tackles"),
    "Aerials Won (%)": ("Aerials Won", "Aerials"),
    "Duels Won (%)": ("Duels Won", "Total Duels"),
    "Long Balls Won (%)": ("Long Balls Won", "Long Balls"),
    "Accurate Passes (%)": ("Accurate Passes", "Passes"),
    "Accurate Crosses (%)": ("Accurate Crosses", "Total Crosses"),
    "Shot Quality (%)": ("Expected Goals", "Shots Total"),
    "On Target Shot Quality (%)": ("Expected Goals On Target", "Shots On Target"),
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
        if not valid_metric(original_name, item["value"]):
            continue
        category = mapping.get(original_name)
        if category:
            group, canonical_name = category
            item["name"] = canonical_name
            item["source_name"] = original_name
            categories[group].append(item)
        else:
            extras.append(item)
    if mapping is TEAM_METRIC_CATEGORY:
        zero_fill_counts(categories, set(mapping.values()))
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
    value = percentage(numerator, denominator)
    if name in {"Assist Efficiency (%)", "Shot Quality (%)", "On Target Shot Quality (%)"}:
        if numerator is not None and denominator is not None and numerator >= 0 and denominator > 0:
            value = min(100.0, numerator / denominator * 100)
    if name == CROSSING_RATE:
        rows[:] = normalize_crossing_rows(rows)
        # Known counts with missing attempts cannot use a provider-only fallback.
    # Other derived percentages retain their existing invalid-count behaviour.
    rows[:] = [row for row in rows if row.get("name") != name]
    if value is not None:
        rows.append(_metric(name, round(value, 1)))


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
    aerial_counts = {item["name"]: item.get("value")
                     for rows in categories.values() for item in rows}
    aerial_pair = rate_counts("Aerials Won (%)", aerial_counts, "Aerials Won", "Aerials")
    _upsert_percentage(defending, "Aerials Won (%)", *(aerial_pair or (None, None)))
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
    points = []
    for name in ("Shots Total", "Ball Possession %", "Accurate Passes (%)"):
        left, right = hi.get(name), ai.get(name)
        if not (valid_metric(name, left, nonzero=True) and valid_metric(name, right, nonzero=True)):
            continue
        if lang == "tr":
            if name == "Shots Total":
                points.append(f"Toplam şut sayısı {home_name} için {left:g}, {away_name} için {right:g} oldu.")
            elif name == "Ball Possession %":
                points.append(f"Topa sahip olma dağılımı {home_name} %{left:g}, {away_name} %{right:g} olarak gerçekleşti.")
            else:
                points.append(f"Pas başarısı {home_name} için %{left:g}, {away_name} için %{right:g} oldu.")
        elif name == "Shots Total":
            points.append(f"{home_name} recorded {left:g} total shots and {away_name} recorded {right:g}.")
        elif name == "Ball Possession %":
            points.append(f"Possession finished at {left:g}% for {home_name} and {right:g}% for {away_name}.")
        else:
            points.append(f"Pass accuracy was {left:g}% for {home_name} and {right:g}% for {away_name}.")
    if lang == "tr":
        red_events = [event for event in events if str(event.get("type", "")).lower() == "redcard"]
        if red_events:
            points.append(f"Maçta {len(red_events)} kırmızı kart olayı kaydedildi.")
    return points


def _select_match_players(
    teams: list[dict[str, Any]], lineups: list[dict[str, Any]], events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    def find_metric(player: dict[str, Any], name: str) -> float | None:
        for metric in (player.get("categories") or {}).get("contribution_impact") or []:
            if metric.get("name") == name:
                try:
                    return float(metric.get("value"))
                except (TypeError, ValueError):
                    return None
        return None

    selected: dict[str, list[dict[str, Any]]] = {}
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
        selected[str(team.get("id"))] = [{
            "player_id": player.get("player_id"),
            "player_name": player.get("player_name"),
            "selection_type": selection_type,
        } for player, selection_type in chosen_players]
    return selected


def _build_report_overview_summary(report: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    teams = report.get("teams") or []
    has_pressure = bool(report.get("pressure"))
    has_territory = bool(report.get("ball_coordinates"))
    from match_analysis_module.regional_play import match_regional_perspective

    pressure_bands = defaultdict(list)
    for row in report.get("pressure") or []:
        minute = row.get("minute")
        if isinstance(minute, (int, float)) and isinstance(row.get("value"), (int, float)):
            period = max(0, int(minute) - 1) // 15
            pressure_bands[row.get("team_id"), period].append(row["value"])
    key_metrics = {"Shots Total", "Shots On Target", "Ball Possession %", "Passes",
                   "Accurate Passes (%)", "Key Passes", "Big Chances Created", "Dangerous Attacks",
                   "Interceptions", "Ball Recovery", "Corners", "Fouls", "Expected Goals"}
    team_comparison = {}
    for scope, scope_teams in {"overall": teams, **(report.get("period_teams") or {})}.items():
        team_comparison[scope] = {
            team.get("name"): {name: value for name, value in _team_stat_index(team).items() if name in key_metrics}
            for team in scope_teams
        }

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
        "pressure_by_15_minute_band": [
            {"team_id": team_id, "from": period * 15 + 1, "to": (period + 1) * 15,
             "mean": round(sum(values) / len(values), 2)}
            for (team_id, period), values in pressure_bands.items()
        ],
        "regional_distribution": match_regional_perspective(report, lang) if has_territory else None,
        "team_comparison": team_comparison,
        "team_findings": report.get("match_team_findings"),
        "player_selections": report.get("player_analysis_perspectives"),
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
    categories_tr = ["Maç Kartı", "Kadro ve Diziliş", "Maç Akışı", "Bölgesel Oyun Dağılımı", "Takım Karşılaştırması", "Takım Analizi", "Oyuncu Analizi"]
    categories_en = ["Match Card", "Lineup & Formation", "Timeline", "Regional Play Distribution", "Team Comparison", "Team Analysis", "Player Analysis"]
    if has_pressure:
        categories_tr.insert(3, "Momentum")
        categories_en.insert(3, "Momentum")
    if not has_territory:
        categories_tr.remove("Bölgesel Oyun Dağılımı")
        categories_en.remove("Regional Play Distribution")
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
        section_count = len(categories)
        section_order = ", ".join(categories_en)
        momentum_instruction = (
            "Lineup & Formation and Momentum have exactly two; "
            if has_pressure
            else "Lineup & Formation has exactly two; "
        )
        pressure_guard = (
            " Never mention pressure or momentum because no Pressure Index data is available."
            if not has_pressure
            else ""
        )
        territory_guard = (
            " Never mention regional play distribution, field zones, ball-location concentration, channels, or territory because no ball-coordinate evidence is available."
            if not has_territory
            else ""
        )
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.2).invoke([
            (
                "system",
                f"You are ScoutWise Enterprise's lead football-report editor. Return only a valid JSON array of exactly {section_count} objects, in this exact report-section order: {section_order}. Translate category names naturally into the requested language. Every object must contain exactly category, summary, and sub_bullets. Keep exactly these sections and be highly concise. summary must be a cohesive, evidence-led synthesis of 20-30 words containing only the section's decisive interpretation. Every sub-bullet object must contain exactly label and text, with text limited to 10-18 words. Apply these counts strictly: Match Card, Timeline, and Regional Play Distribution have zero sub-bullets; {momentum_instruction}Team Comparison, Team Analysis, and Player Analysis have exactly two. For Team Comparison and Team Analysis, create one sub-bullet per team and use that team's exact name as the label. For Player Analysis, select one featured player from each team with a recorded selection and use the player's full name as the label; never select two players from the same team. Synthesize the supplied match evidence and findings. Treat pressure averages as relative intensity and do not quote raw pressure scores. State only supported interpretations and what the evidence positively indicates. Never add defensive capability caveats such as 'does not prove', 'cannot determine', 'cannot establish', 'alone is insufficient', 'the data does not show', or their equivalents. In Regional Play Distribution especially, describe the supported spatial pattern, channel use, central versus wide concentration, and proximity to dangerous end areas without explaining what cannot be inferred. Preserve distinctions between observation, correlation, and causation through careful affirmative wording, not disclaimer sentences. Never invent data, attack direction, roles, or events.{pressure_guard}{territory_guard} Avoid repeating the same fact across sections. Use polished, direct football-analysis language without markdown, bullet characters, recommendations, or generic filler." + FOOTBALL_TERMINOLOGY_GUIDANCE,
            ),
            ("human", f"Language: {language}\nReport evidence and existing interpretations:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) != section_count:
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


def generate_match_report(fixture_id: int, lang: str = "en", build_narratives: bool = True) -> dict[str, Any]:
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
        raise MatchReportError(f"{message or 'ScoutWise match data request failed'} (HTTP {response.status_code})")
    fixture = response.json().get("data") or {}
    if not fixture:
        raise MatchReportError("ScoutWise returned empty match data")

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
        _derive_team_percentages(categories)
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
            # Sportmonks repeats full-match possession in the second-half rows.
            # Exclude it from both comparisons and narrative evidence.
            if period_key == "second_half" and (
                row.get("type_id") == 45
                or (row.get("type") or {}).get("name") == "Ball Possession %"
            ):
                continue
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

    _add_match_attack_balances(teams)
    for scoped_teams in period_teams.values():
        _add_match_attack_balances(scoped_teams)

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
    if build_narratives:
        emit_progress(report, "statistics")
        report["player_analysis_perspectives"] = _select_match_players(teams, lineups, events)
        emit_progress(report, "player_analysis_perspectives")
        from match_report_module.team_findings import upgrade_match_team_findings
        report.update(upgrade_match_team_findings(report))
        emit_progress(report, "match_team_findings")
        from match_report_module.style_profiles import upgrade_match_profiles
        enriched = upgrade_match_profiles(report)
        if enriched is not report:
            report["match_profiles"] = enriched["match_profiles"]
            emit_progress(report, "match_profiles")
        report["overview_summary"] = _build_report_overview_summary(report, lang)
        emit_progress(report, "overview_summary")
    report["analysis_model"] = os.getenv(
        "OPENAI_MATCH_REPORT_MODEL",
        os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
    )
    return report


def build_team_report_metrics(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en"
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    match_rows: list[dict[str, Any]] = []
    penalty_samples = []
    for report in reports:
        team = next(
            (row for row in report.get("teams") or [] if int(row.get("id") or 0) == int(team_id)),
            None,
        )
        if not team:
            continue
        match_values: dict[str, dict[str, float]] = {}
        from copy import deepcopy
        sources = deepcopy(team.get("categories") or {})
        zero_fill_counts(sources, set(TEAM_METRIC_CATEGORY.values()))
        if team.get("expected_metrics"):
            sources["expected"] = team.get("expected_metrics") or []
        for group, metrics in sources.items():
            for metric in metrics or []:
                try:
                    value = float(metric.get("value"))
                except (TypeError, ValueError):
                    continue
                name = str(metric.get("name") or "").strip()
                if not name or not valid_metric(name, value):
                    continue
                samples[group][name].append(value)
                if not metric.get("_inferred_zero"):
                    match_values.setdefault(group, {})[name] = value
        penalty_sample = penalty_xg_sample(match_values.get("expected", {}).get(PENALTY_XG), report.get("events") or [], team_id)
        if penalty_sample is not None:
            penalty_samples.append(penalty_sample)
        match_rows.append({
            "fixture": (report.get("fixture") or {}).get("name") or (report.get("fixture") or {}).get("id"),
            "metrics": match_values,
        })
    protected = set().union(*(missing_attempt_names({name: value for items in row["metrics"].values() for name, value in items.items()}) for row in match_rows))
    aggregate: dict[str, list[dict[str, Any]]] = {}
    for group, metrics in samples.items():
        aggregate[group] = []
        for name, values in metrics.items():
            if rate_key(name) in protected:
                continue
            if group == "expected" and name == PENALTY_XG:
                average = penalty_xg_average(penalty_samples)
                if average is not None:
                    aggregate[group].append({"name": name, "value": average, "perMatch": average,
                                             "aggregation": "average", "matchesCovered": len(penalty_samples),
                                             "attempts": sum(count for _, count in penalty_samples)})
                continue
            is_average = "%" in name or "percentage" in name.casefold() or "performance" in name.casefold()
            total = sum(values)
            numerator_denominator = DERIVED_PERCENTAGE_METRICS.get(name)
            pooled = pooled_rate_counts(name, [({name: value for values in row["metrics"].values() for name, value in values.items()}, 1) for row in match_rows],
                                        *numerator_denominator) if numerator_denominator else None
            derived_rate = percentage(pooled[0], pooled[1]) if pooled else None
            if numerator_denominator and derived_rate is None:
                continue
            aggregate[group].append({
                "name": name,
                "value": round(derived_rate, 2) if derived_rate is not None else round(total / len(values), 2) if is_average else round(total, 2),
                "perMatch": round(derived_rate, 2) if derived_rate is not None else round(total / len(values), 2),
                "aggregation": "derived_rate" if derived_rate is not None else "average" if is_average else "total",
                "matchesCovered": int(pooled[2]) if pooled else len(values),
            })
        # Keep zero samples in the calculations, but hide zero aggregate rows.
        aggregate[group] = sorted(
            (row for row in aggregate[group] if valid_metric(row["name"], row["value"], nonzero=True)),
            key=lambda row: row["name"],
        )
    from standings_module.team_derived import DEFINITIONS
    derived = _team_profile_derived_evidence(reports, team_id, {name: definition[1] for name, definition in DEFINITIONS.items()})
    added_names = {row["name"] for rows in derived.values() for row in rows}
    aggregate = {group: [row for row in rows if row["name"] not in added_names] for group, rows in aggregate.items()}
    for group, rows in derived.items():
        aggregate.setdefault(group, []).extend({
            "name": row["name"], "value": row["value"], "perMatch": row["valuePerMatch"],
            "aggregation": row["aggregation"], "matchesCovered": row["matchesCovered"],
            "preference": row["preference"],
        } for row in rows)
    aggregate = {group: rows for group, rows in aggregate.items() if rows}
    # Narrative evidence is filtered after arithmetic, so zero samples still
    # contribute to denominators without becoming analytical highlights.
    narrative_match_rows = []
    for row in match_rows:
        metrics = {group: values for group, values in nonzero_evidence(row["metrics"]).items() if values}
        if metrics:
            narrative_match_rows.append({"fixture": row["fixture"], "metrics": metrics})
    emit_progress({"teamMetrics": aggregate}, "teamMetrics")
    available = list(aggregate)
    fallback_text = (
        "Seçili maçların toplu değerleri, takımın bu kategorideki üretimini ve maç başına seviyesini birlikte gösteriyor."
        if lang == "tr" else
        "The selected-match totals and per-match levels show the team's output in this category."
    )
    fallback = {group: fallback_text for group in available}
    if not available or not os.getenv("OPENAI_API_KEY"):
        return aggregate, fallback
    try:
        from langchain_openai import ChatOpenAI
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior football team analyst. Return only one valid JSON object whose top-level keys MUST be copied character-for-character from REQUIRED_JSON_KEYS. Never translate, rename, title-case or omit those machine keys. Translate metric concepts only inside each string value. For every required key, write one evidence-led interpretation of 50-75 words in the requested language. Analyse only the named team across the selected matches. Opponent-derived fields describe production by the opponents faced in those same matches; interpret that exposure as part of the named team's profile, never as another team report or an external benchmark. The output must prioritise football conclusions, not recite the data: use at most two short numerical facts as evidence, then immediately explain what their relationship indicates about efficiency, consistency, risk, control, or recurring performance structure. Never state both the total and per-match version of the same metric; choose whichever is more informative. Do not enumerate metrics, repeat card values, report coverage counts, or walk through match-by-match ranges unless that variation directly supports the main conclusion. When the language is Turkish, every football term inside the values must be natural Turkish; the required JSON keys are the sole exception because they are machine identifiers and are not displayed. Use 2-3 cohesive sentences, with the evidence brief and the interpretation dominant. Never invent causation, tactical intent, external benchmarks, comparisons with unprovided opponents, or unavailable facts. For opponent_threat, lower values are preferable; relate allowed volumes, opportunity quality and success rates without calling a value high or low without supplied comparative evidence. For opponent_profile, interpret opponent tendencies as neutral context, never automatic superiority or weakness. Signed own-minus-opponent differences may be negative; Shot Share (%) is pooled across paired matches and is not possession share. xGD is provider-supplied, never reconstructed. Interpret every required category using all available relevant metrics, prioritising a supported football conclusion over metric definitions or generic data disclaimers. Do not use markdown or headings." + FOOTBALL_TERMINOLOGY_GUIDANCE),
            ("human", f"Language: {language}\nREQUIRED_JSON_KEYS: {json.dumps(available, ensure_ascii=False)}\nAggregated team categories: {json.dumps(aggregate, ensure_ascii=False)}\nNon-zero per-match team values: {json.dumps(narrative_match_rows, ensure_ascii=False)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("team perspective response root was not a JSON object")
        missing = [group for group in available if not str(parsed.get(group) or "").strip()]
        if missing:
            print(f"[enterprise_team_report] event=perspective_partial_fallback missing_keys={missing}")
        return aggregate, {group: str(parsed.get(group) or fallback[group]).strip() for group in available}
    except Exception as exc:
        print(f"[enterprise_team_report] event=perspective_fallback error={exc}")
        return aggregate, fallback


def _team_player_metric_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare player evidence without changing raw single-match reports.

    Infer omitted counts only when another participant has that metric in the
    same fixture. Advanced metrics stay unknown. Require 90 covered minutes
    and observations in 20% of selected matches, rounded up and capped at three.
    Rates count only fixtures with usable inputs and a positive denominator.
    """
    from copy import deepcopy
    from math import isfinite

    reports = deepcopy(reports)
    minimum_matches = min(3, max(1, (len(reports) + 4) // 5))
    players = defaultdict(list)

    def sources(row):
        return {**(row.get("categories") or {}), "expected": row.get("expected_metrics") or []}

    def numeric(value):
        try:
            result = float(value)
            return result if isfinite(result) else None
        except (TypeError, ValueError):
            return None

    for report in reports:
        participants = []
        covered = set()
        for row in report.get("lineups") or []:
            metrics = {(group, str(item.get("name") or "")): item
                       for group, items in sources(row).items() for item in items}
            minutes = next((numeric(item.get("value")) for (_, name), item in metrics.items()
                            if name == "Minutes Played"), None)
            if not minutes or minutes <= 0:
                continue
            participants.append((row, metrics, minutes))
            covered.update(key for key, item in metrics.items()
                           if is_standard_count(key[1], key[0])
                           and numeric(item.get("value")) is not None)
        for row, metrics, minutes in participants:
            protected = missing_attempt_names({name: item.get("value") for (_, name), item in metrics.items() if not item.get("_inferred_zero")})
            for group, name in covered - metrics.keys():
                if rate_key(name) in protected:
                    continue
                item = {"name": name, "value": 0.0, "_inferred_zero": True}
                row.setdefault("categories", {}).setdefault(group, []).append(item)
                metrics[group, name] = item
            players[(row.get("team_id"), row.get("player_id") or row.get("player_name"))].append((row, metrics, minutes))

    for samples in players.values():
        coverage = defaultdict(float)
        match_coverage = Counter()
        for _, metrics, minutes in samples:
            for key, item in metrics.items():
                if numeric(item.get("value")) is not None:
                    coverage[key] += minutes
                    match_coverage[key] += 1
        protected = set().union(*(missing_attempt_names({name: item.get("value") for (_, name), item in metrics.items() if not item.get("_inferred_zero")}) for _, metrics, _ in samples))
        for group, metric in list(coverage):
            if rate_key(metric) in protected:
                coverage[group, metric] = 0
        # Rate inputs retain recorded fixture coverage independently of volume counts.
        count_samples = [({name: item.get("value") for (_, name), item in metrics.items() if not item.get("_inferred_zero")}, minutes)
                         for _, metrics, minutes in samples]
        for name, components in DERIVED_PERCENTAGE_METRICS.items():
            pooled = pooled_rate_counts(name, count_samples, *components)
            covered_minutes = pooled[2] if pooled else 0
            covered_matches = sum(
                single is not None and single[1] > 0
                for counts, _ in count_samples
                for single in [pooled_rate_counts(name, [(counts, 1)], *components)]
            ) if pooled else 0
            value = percentage(pooled[0], pooled[1]) if (
                pooled and covered_minutes >= 90 and covered_matches >= minimum_matches
            ) else None
            group = next((group for group, canonical in PLAYER_METRIC_CATEGORY.values()
                          if canonical == components[0]), None)
            if "Shot Quality" in name:
                group = "shooting"
            if group is None:
                continue
            for row, metrics, _ in samples:
                item = metrics.get((group, name))
                if item is None and value is not None:
                    item = {"name": name, "value": value}
                    row.setdefault("categories", {}).setdefault(group, []).append(item)
                    metrics[group, name] = item
                elif item is not None:
                    item["value"] = value
            coverage[group, name] = covered_minutes if value is not None else 0
            match_coverage[group, name] = covered_matches if value is not None else 0
        for row, metrics, _ in samples:
            for group, items in sources(row).items():
                filtered = [item for item in items if item.get("name") == "Minutes Played"
                            or (coverage[group, str(item.get("name") or "")] >= 90
                                and (item.get("name") == "Rating"
                                     or match_coverage[group, str(item.get("name") or "")] >= minimum_matches))]
                for item in filtered:
                    item.pop("_inferred_zero", None)
                if group == "expected":
                    row["expected_metrics"] = filtered
                else:
                    row["categories"][group] = filtered
    return reports


def build_team_report_player_perspectives(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en", *, include_ai: bool = True
) -> dict[str, dict[str, str]]:
    reports = _team_player_metric_reports(reports)
    players: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report.get("lineups") or []:
            if int(row.get("team_id") or 0) != int(team_id):
                continue
            contribution = (row.get("categories") or {}).get("contribution_impact") or []
            values = {str(item.get("name")): item.get("value") for item in contribution}
            try:
                minutes = float(values.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            key = str(row.get("player_id") or row.get("player_name"))
            player = players.setdefault(key, {
                "player_id": row.get("player_id"), "player_name": row.get("player_name"),
                "position": row.get("position_name"), "minutes": 0.0,
                "rating_weighted": 0.0, "rating_minutes": 0.0,
                "metrics": defaultdict(lambda: defaultdict(list)),
            })
            player["minutes"] += minutes
            try:
                rating = float(values.get("Rating"))
                player["rating_weighted"] += rating * minutes
                player["rating_minutes"] += minutes
            except (TypeError, ValueError):
                pass
            sources = dict(row.get("categories") or {})
            if row.get("expected_metrics"):
                sources["expected"] = row.get("expected_metrics") or []
            for group, metrics in sources.items():
                for metric in metrics or []:
                    try:
                        value = float(metric.get("value"))
                    except (TypeError, ValueError):
                        continue
                    player["metrics"][group][str(metric.get("name"))].append((value, minutes))
    eligible = []
    for player in players.values():
        if player["minutes"] < 90 or not player["rating_minutes"]:
            continue
        player["rating"] = player["rating_weighted"] / player["rating_minutes"]
        compact_metrics = {}
        for group, metrics in player["metrics"].items():
            compact_metrics[group] = {}
            for name, samples in metrics.items():
                average = "%" in name or any(word in name.casefold() for word in ("rating", "percentage", "performance", "captain"))
                available_minutes = sum(minutes for _, minutes in samples)
                compact_metrics[group][name] = round(
                    sum(value * minutes for value, minutes in samples) / available_minutes
                    if average and available_minutes else
                    sum(value for value, _ in samples) * 90 / available_minutes
                    if available_minutes else 0,
                    2,
                )
        player["metrics"] = compact_metrics
        eligible.append(player)
    ordered = sorted(eligible, key=lambda player: (-player["rating"], -player["minutes"], str(player["player_name"])))
    featured = ordered[:3]
    featured_ids = {str(player["player_id"] or player["player_name"]) for player in featured}
    development = [player for player in reversed(ordered) if str(player["player_id"] or player["player_name"]) not in featured_ids][:3]
    chosen = [(player, "featured") for player in featured] + [(player, "development") for player in development]
    result = {
        str(player["player_id"] or player["player_name"]): {
            "selectionType": selection,
            "text": (
                f"{player['player_name']}, seçili maçlarda {player['rating']:.2f} ortalama puanla öne çıkan performanslardan birini verdi. Mevki rolündeki üretimi bu seçimi destekledi."
                if selection == "featured" and lang == "tr" else
                f"{player['player_name']}, seçili maçlardaki {player['rating']:.2f} ortalama puanıyla gelişime açık performanslar arasında kaldı. Mevki sorumluluklarındaki düşük kalan göstergeler temel gelişim alanını oluşturdu."
                if lang == "tr" else
                f"{player['player_name']} produced one of the standout selected-match performances with a {player['rating']:.2f} average score. The player's positional output supports the selection."
                if selection == "featured" else
                f"{player['player_name']} remained among the development-area performers with a {player['rating']:.2f} selected-match average. Lower position-relevant output defines the main development area."
            ),
        }
        for player, selection in chosen
    }
    if not include_ai or not chosen or not os.getenv("OPENAI_API_KEY"):
        return result
    compact = [{
        "player_id": player["player_id"], "player_name": player["player_name"],
        "selection_type": selection, "position": player["position"],
        "minutes": round(player["minutes"]), "average_rating": round(player["rating"], 2),
        "aggregated_selected_match_metrics": nonzero_evidence(player["metrics"]),
    } for player, selection in chosen]
    try:
        from langchain_openai import ChatOpenAI
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior player-performance analyst. Return only a valid JSON object keyed by player_id. Each value must be a single-paragraph 30-40 word interpretation in the requested language. Use the same methodology throughout. For featured players, write an exclusively positive, evidence-led assessment emphasizing the strongest position-relevant aggregated metrics and their football meaning; never mention a weakness. For development players, write an exclusively weakness-focused diagnosis using low efficiency, errors, discipline, lost actions, missed chances, or low position-relevant output; never praise or soften. Never cite zero-valued metrics. Use only supplied evidence, lead with the conclusion, keep numerical facts brief, and prioritise interpretation over listing. Translate all metric concepts naturally; in Turkish never use English metric names and write percentages as %42. Never invent tactics, causation, benchmarks, or recommendations. No markdown or headings." + FOOTBALL_TERMINOLOGY_GUIDANCE),
            ("human", f"Language: {language}\nSelected players and aggregated selected-match data:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        for player, selection in chosen:
            key = str(player["player_id"] or player["player_name"])
            value = parsed.get(key) or parsed.get(str(player["player_id"]))
            if isinstance(value, dict):
                value = value.get("text")
            if str(value or "").strip():
                result[key] = {"selectionType": selection, "text": str(value).strip()}
    except Exception as exc:
        print(f"[enterprise_team_report] event=player_perspective_fallback error={exc}")
    return result


def build_team_report_momentum_perspectives(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en"
) -> dict[str, str]:
    halves: dict[str, list[dict[str, Any]]] = {"firstHalf": [], "secondHalf": []}
    for report in reports:
        pressure = report.get("pressure") or []
        team_rows = {int(row.get("minute") or 0): float(row.get("value") or 0) for row in pressure if int(row.get("team_id") or 0) == int(team_id)}
        opponent_rows: dict[int, float] = defaultdict(float)
        for row in pressure:
            if int(row.get("team_id") or 0) != int(team_id):
                opponent_rows[int(row.get("minute") or 0)] += float(row.get("value") or 0)
        minutes = sorted(set(team_rows) | set(opponent_rows))
        events = report.get("events") or []
        for key, start, end in (("firstHalf", 0, 45), ("secondHalf", 46, 120)):
            net = [(minute, team_rows.get(minute, 0) - opponent_rows.get(minute, 0)) for minute in minutes if start <= minute <= end]
            if not net:
                continue
            event_rows = []
            for event in events:
                minute = int(event.get("minute") or 0)
                event_type = str(event.get("type") or "").casefold()
                if start <= minute <= end and ("goal" in event_type or "substitution" in event_type):
                    event_rows.append({"minute": minute, "type": event.get("type"), "forTeam": int(event.get("team_id") or 0) == int(team_id)})
            halves[key].append({
                "fixture": (report.get("fixture") or {}).get("name") or (report.get("fixture") or {}).get("id"),
                "averageNetPressure": round(sum(value for _, value in net) / len(net), 2),
                "dominantMinuteShare": round(sum(1 for _, value in net if value > 0) / len(net) * 100, 1),
                "strongestMinutes": sorted(net, key=lambda row: row[1], reverse=True)[:5],
                "weakestMinutes": sorted(net, key=lambda row: row[1])[:5],
                "events": event_rows,
            })
    fallback = {
        "firstHalf": "İlk yarı baskı akışı seçili maçlar boyunca takımın üstünlük kurduğu ve kontrolü rakibe bıraktığı dakika aralıklarını birlikte gösteriyor." if lang == "tr" else "The first-half pressure flow shows the intervals in which the team established and surrendered control across the selected matches.",
        "secondHalf": "İkinci yarı baskı akışı, devre sonrası baskı sürekliliğini ve gol ya da oyuncu değişiklikleri çevresindeki tekrar eden yön değişimlerini öne çıkarıyor." if lang == "tr" else "The second-half pressure flow highlights post-break continuity and recurring changes around goals or substitutions.",
    }
    if not any(halves.values()) or not os.getenv("OPENAI_API_KEY"):
        return fallback


    try:
        from langchain_openai import ChatOpenAI
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior football momentum analyst. Return only valid JSON with exactly firstHalf and secondHalf. Write 40-60 words for each in the requested language. Analyse the named team's recurring pressure pattern across all selected matches, not a chronological retelling of every match. Identify the clearest repeated dominant and lost-control minute bands, changes after goals or substitutions when supported, and whether pressure was sustained or fragmented. Treat relationships as coincidence, never proven causation. Do not mention raw pressure-index values, red cards, unavailable tactics, recommendations, or opponent names. Keep facts brief and make the football inference dominant. In Turkish use natural terminology and no English metric names. No markdown or headings." + FOOTBALL_TERMINOLOGY_GUIDANCE),
            ("human", f"Language: {language}\nAggregated first/second-half momentum evidence:\n{json.dumps(halves, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        return {key: str(parsed.get(key) or fallback[key]).strip() for key in fallback}
    except Exception as exc:
        print(f"[enterprise_team_report] event=momentum_perspective_fallback error={exc}")
        return fallback


def build_team_report_regional_perspective(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en"
) -> str:
    from match_analysis_module.regional_play import aggregate_team_regional_play, team_regional_perspective

    name = next((team.get("name") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == team_id), str(team_id))
    return team_regional_perspective(aggregate_team_regional_play(reports, team_id), name, lang)


def _team_profile_derived_evidence(
    reports: list[dict[str, Any]], team_id: int, selected: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """Reuse league formulas over selected paired reports, exposing only selected fields."""
    from standings_module.team_derived import DEFINITIONS, build_team_derived

    sources = {DEFINITIONS[name][0] for name in selected} | set(selected)
    for name in list(sources):
        sources.update(DERIVED_PERCENTAGE_METRICS.get(name, ()))
    paired_rows = []
    for report in reports:
        teams = report.get("teams") or []
        if len(teams) != 2 or not any(int(team.get("id") or 0) == int(team_id) for team in teams):
            continue
        paired = {}
        for team in teams:
            values = {}
            rows = [metric for items in (team.get("categories") or {}).values() for metric in items or []]
            rows.extend(team.get("expected_metrics") or [])
            rows.extend(team.get("extra_metrics") or [])
            for metric in rows:
                name = str(metric.get("name") or "")
                name = TEAM_METRIC_CATEGORY.get(name, (None, name))[1]
                if name not in sources or metric.get("_inferred_zero"):
                    continue
                # Preserve explicit unknowns so a recorded attempt denominator
                # cannot turn an unknown success count into an inferred zero.
                value = float(metric["value"]) if valid_metric(name, metric.get("value")) else None
                value = value if value is not None and (value >= 0 or name == "Expected Goals Difference (xGD)") else None
                if name not in values or values[name] is None:
                    values[name] = value
            paired[str(team["id"])] = {"metrics": values}
        paired_rows.append({"team_contributions": paired})
    metrics = build_team_derived(paired_rows)["teams"].get(str(team_id), {})
    evidence = defaultdict(list)
    for name, category in selected.items():
        metric = metrics.get(name)
        if metric and valid_metric(name, metric["perMatch"], nonzero=True):
            evidence[category].append({"name": name, "valuePerMatch": metric["perMatch"],
                                       "isRate": metric["aggregation"] == "derived_rate", "value": metric["value"],
                                       "aggregation": metric["aggregation"],
                                       "matchesCovered": metric["matchesCovered"], "preference": DEFINITIONS[name][2]})
    return dict(evidence)


def _team_attack_balance_evidence(reports: list[dict[str, Any]], team_id: int) -> dict[str, list[dict[str, Any]]]:
    from standings_module.team_derived import BALANCES

    return _team_profile_derived_evidence(reports, team_id, {
        **{name: category for name, (_, category, _) in BALANCES.items()}, "Shot Share (%)": "shooting",
    })


def _team_defense_threat_evidence(reports: list[dict[str, Any]], team_id: int) -> dict[str, list[dict[str, Any]]]:
    names = (
        "Opponent Dangerous Attacks", "Opponent Big Chances Created", "Opponent Shots Total",
        "Opponent Shots On Target", "Opponent Shots Insidebox", "Expected Goals Against (xGA)",
        "Expected Goals On Target Against (xGoTA)", "Opponent Key Passes",
        "Opponent Accurate Crosses", "Opponent Accurate Crosses (%)",
        "Opponent Successful Dribbles", "Opponent Dribble Accuracy (%)",
    )
    return _team_profile_derived_evidence(reports, team_id, dict.fromkeys(names, "opponent_threat"))



def _add_match_attack_balances(teams: list[dict[str, Any]]) -> None:
    """Add only paired, nonzero balance metrics for this match or this half."""
    from standings_module.team_derived import BALANCES

    names = set(BALANCES) | {"Shot Share (%)"}
    if len(teams) != 2 or len({team.get("id") for team in teams}) != 2:
        return
    additions = {str(team["id"]): _team_attack_balance_evidence([{"teams": teams}], team["id"]) for team in teams}
    common = set.intersection(*[{row["name"] for rows in by_group.values() for row in rows}
                                for by_group in additions.values()])
    for team in teams:
        categories = team.setdefault("categories", {})
        for group, rows in list(categories.items()):
            categories[group] = [row for row in rows if row.get("name") not in names]
        for group, rows in additions[str(team["id"])].items():
            categories.setdefault(group, []).extend({**_metric(row["name"], row["valuePerMatch"]), "preference": "higher"}
                                                   for row in rows if row["name"] in common)


def build_team_report_attack_profile(
    reports: list[dict[str, Any]],
    team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]],
    lang: str = "en",
    *, include_analysis: bool = True, minimum_style_metrics: int = 4,
) -> dict[str, Any]:
    from report_module.attacking_styles import STYLE_VERSION, select_team_styles, bind_selected_styles
    selected_styles = select_team_styles(reports, team_id, lang, minimum_metrics=minimum_style_metrics)
    attacking_groups = ("contribution_impact", "shooting", "passing", "expected")
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")
    team_evidence = {
        group: [{
            "name": row.get("name"),
            "valuePerMatch": row.get("perMatch"),
            "isRate": "%" in str(row.get("name") or "") or "percentage" in str(row.get("name") or "").casefold(),
        } for row in rows if valid_metric(str(row.get("name") or ""), row.get("perMatch"), nonzero=True)]
        for group, rows in team_metrics.items()
        if group in attacking_groups and rows
    }
    for group, metrics in _team_attack_balance_evidence(reports, team_id).items():
        added_names = {metric["name"] for metric in metrics}
        team_evidence[group] = [metric for metric in team_evidence.get(group, []) if metric["name"] not in added_names] + metrics
    reports = _team_player_metric_reports(reports)
    players: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report.get("lineups") or []:
            if int(row.get("team_id") or 0) != int(team_id):
                continue
            impact = {
                str(item.get("name")): item.get("value")
                for item in (row.get("categories") or {}).get("contribution_impact", []) or []
            }
            try:
                minutes = float(impact.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            key = str(row.get("player_id") or row.get("player_name"))
            player = players.setdefault(key, {
                "playerId": row.get("player_id"), "name": row.get("player_name"),
                "imageUrl": row.get("player_image_url"), "position": row.get("position_name"),
                "minutes": 0.0, "ratingWeighted": 0.0, "ratingMinutes": 0.0,
                "metrics": defaultdict(lambda: defaultdict(list)),
            })
            player["minutes"] += minutes
            if not player.get("imageUrl") and row.get("player_image_url"):
                player["imageUrl"] = row.get("player_image_url")
            try:
                rating = float(impact.get("Rating"))
                player["ratingWeighted"] += rating * minutes
                player["ratingMinutes"] += minutes
            except (TypeError, ValueError):
                pass
            sources = dict(row.get("categories") or {})
            sources["expected"] = row.get("expected_metrics") or []
            for group in attacking_groups:
                for metric in sources.get(group) or []:
                    try:
                        value = float(metric.get("value"))
                    except (TypeError, ValueError):
                        continue
                    player["metrics"][group][str(metric.get("name") or "")].append((value, minutes))
    candidates: list[dict[str, Any]] = []
    for player in players.values():
        if player["minutes"] < 90:
            continue
        position_name = str(player.get("position") or "").casefold()
        if "goalkeeper" in position_name or "kaleci" in position_name:
            continue
        compact: dict[str, dict[str, float]] = {}
        attack_score = 0.0
        for group, metrics in player["metrics"].items():
            compact[group] = {}
            for name, samples in metrics.items():
                normalized = name.casefold()
                average = "%" in name or any(word in normalized for word in ("rating", "percentage", "performance"))
                raw_value = (
                    sum(value * minutes for value, minutes in samples) / sum(minutes for _, minutes in samples)
                    if average else sum(value for value, _ in samples)
                )
                value = raw_value if average else raw_value / sum(minutes for _, minutes in samples) * 90
                if not valid_metric(name, round(value, 2), nonzero=True):
                    continue
                compact[group][name] = {"value": round(value, 2), "basis": "rate" if is_percentage(name) else "average" if average else "per90"}
                if any(word in normalized for word in ("goal", "assist", "shot", "key pass", "chance", "expected goal", "expected assist", "xg", "xa")):
                    weight = 5 if "goal" in normalized and "expected" not in normalized else 3 if "assist" in normalized else 1
                    attack_score += max(0, raw_value) * weight
        rating = player["ratingWeighted"] / player["ratingMinutes"] if player["ratingMinutes"] else 0
        role_bonus = 2.0 if any(role in position_name for role in ("attacker", "forward", "winger", "hücum")) else 1.0 if any(role in position_name for role in ("midfielder", "orta saha")) else 0.0
        player.update({"minutes": round(player["minutes"]), "averageRating": round(rating, 2), "metrics": compact, "attackScore": round(attack_score + rating + role_bonus, 2)})
        candidates.append(player)
    candidates.sort(key=lambda row: (-row["attackScore"], -row["averageRating"], -row["minutes"]))
    shortlist = candidates[:8]
    def metric_cell(name: str, value: Any, basis: str = "per90") -> dict[str, str]:
        try:
            numeric = float(value)
            shown = str(int(numeric)) if numeric.is_integer() else f"{numeric:.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            shown = str(value or "—")
        return {"label": name, "value": f"%{shown}" if basis == "rate" else shown}

    if not include_analysis:
        from .team_profile_comparison import visual_profile
        return visual_profile(selected_styles, shortlist, metric_cell, STYLE_VERSION, 'attack')

    flat_team = [
        (group, str(row.get("name") or ""), row.get("valuePerMatch"), "rate" if row.get("isRate") else "perMatch")
        for group, rows in team_evidence.items() for row in rows
        if row.get("name") and row.get("valuePerMatch") is not None
    ]
    if not team_evidence and not selected_styles:
        return {"themes": [], "players": []}
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Report generation is not configured")
    try:
        language = "Turkish" if lang == "tr" else "English"
        compact_players = [{key: value for key, value in player.items() if key not in ("ratingWeighted", "ratingMinutes")} for player in shortlist]
        parsed = generate_profile_json([
            ("system", "You are ScoutWise Enterprise's senior professional attacking analyst. Return only valid JSON with exactly themes and players. Role-group shares are percentages of recorded outfield contributions to the SAME task; never compare different tasks as equivalent. Shots Per 100 Passes is shots per 100 recorded passes. Team volume metrics are per-match averages; player volume metrics are normalized per 90 minutes. Do not mix these bases. NEVER write '/90' or repeatedly mention normalization in any metric cell or analysis; show only the numeric value. Rate metrics retain percentages. Ignore set-piece data entirely. The supplied Dangerous Attacks Difference, Shots Difference and Shots On Target Difference are own output minus the opponent output within the same selected matches, averaged over paired recorded matches; negative values are valid and higher values indicate a more favourable balance. Shot Share (%) is own shots divided by both teams combined shots, pooled over the same selected matches, not an average of match percentages. These are team-only contextual indicators of production balance, not proof of possession control or chance quality. Consider them alongside the existing attacking data when supported; do not force them into every theme or assign them to individual players. themes must contain exactly the supplied selected attacking styles, in the supplied order (normally two). Copy each styleId and title exactly; never invent, rename, replace, reorder or add a style. These identities were selected deterministically with the coach-report classifier. Each theme must have styleId, title, metrics, analysis and evidence_metrics. Use only that selected style's source_metrics for its metric cells and evidence_metrics. The readingBoundary limits the claims supported by the recorded statistics. Interpret each selected style using the relationships in its own diagnostic metrics and relevant team context. Theme metrics must contain exactly four objects with label and value, and EVERY theme metric cell must contain exactly one metric label and one numeric value—never combine multiple metrics in a cell. Theme analysis must contain exactly TWO professional bullet strings, each 40-50 words. Start with a direct team-specific conclusion about how the team creates, progresses or finishes; never define a metric and never write generic statements. Use the entire supplied dataset internally, but do not recite it: each analysis bullet may contain at most ONE short numerical fact and at least 80% of its wording must be football inference. Never write sequences such as '37 passes, 30 accurate passes, 81%, 2 final-third passes'. Explain what the relationships reveal about recurring attacking routes, dependencies, efficiency, risk, chance quality, conversion and threat variety. players must contain exactly two players selected strictly through attacking contribution. For every player copy playerId exactly from the supplied shortlist and return playerId, metrics and analysis. Each player must have exactly four role-specific metric cells, each containing exactly one metric label and one numeric value. Choose different cells when player roles differ. Player analysis must consider all supplied relevant metrics—including those not displayed—but contain exactly TWO bullets of 20-30 words each, with at most ONE short numerical fact per bullet. Lead with what the player does in this team's attack, the decisions or spaces the role affects, and the dependency or advantage created; do not narrate a statistical line. Do not mention match count, opponents, unavailable tactics, external benchmarks, coverage counts, raw category names or recommendations. Copy metric labels exactly only in metric-cell label fields because the UI translates those fields; translate metric references inside narrative analysis into the requested language. In Turkish analysis translate Touches exactly as 'Topla Buluşma' and use 'başarı oranı', never 'doğruluk'; discuss advanced metrics with their full Turkish name followed by the correct abbreviation in parentheses, for example 'Akan Oyun Gol Beklentisi (xGOP)'. Preserve distinct xG, xA, xGoT, npxG, xGOP and other supplied advanced metrics. Never output markdown." + FOOTBALL_TERMINOLOGY_GUIDANCE + PROFILE_EVIDENCE_GUIDANCE),
            ("human", f"Language: {language}\nTeam: {team_name}\nAggregated team attacking metrics: {json.dumps(team_evidence, ensure_ascii=False)}\nSelected attacking styles with authoritative evidence: {json.dumps(selected_styles, ensure_ascii=False)}\nEligible attacking-player shortlist: {json.dumps(compact_players, ensure_ascii=False, default=str)}"),
        ], require_players=True)
        team_source = {name: metric_cell(name, value, basis) for _, name, value, basis in flat_team}
        player_sources = [{
            **{key: player[key] for key in ("playerId", "name", "imageUrl", "position", "minutes", "averageRating") if key in player},
            "source_metrics": {name: metric_cell(name, detail["value"], detail["basis"]) for rows in player["metrics"].values() for name, detail in rows.items()},
        } for player in shortlist]
        result = build_evidence_profile({**parsed, 'themes': []}, team_source, player_sources, theme_limit=4)
        result['themes'] = bind_selected_styles(parsed, selected_styles)
        result['styleVersion'] = STYLE_VERSION
        return result
    except Exception as exc:
        print("[enterprise_team_report] event=profile_generation_failed "
              f"section=build_team_report_attack_profile error_type={type(exc).__name__}")
        raise




def build_team_report_defense_profile(
    reports: list[dict[str, Any]],
    team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]],
    lang: str = "en",
    *, include_analysis: bool = True, minimum_style_metrics: int = 4,
) -> dict[str, Any]:
    """Build a selected-match defensive identity without goalkeeper-save evidence."""
    from report_module.attacking_styles import DEFENCE_STYLE_VERSION as STYLE_VERSION, select_team_styles, bind_selected_styles
    selected_styles = select_team_styles(reports, team_id, lang, side='defence', minimum_metrics=minimum_style_metrics)
    groups = ("defending", "errors_discipline")
    excluded = (
        "save", "kurtar", "offside", "ofsayt", "shot off target",
        "shots off target", "big chances missed", "missed big chances",
        "inaccurate shot", "goal conversion", "chance created",
    )
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")

    def allowed(name: Any) -> bool:
        return bool(name) and not any(word in str(name).casefold() for word in excluded)

    team_evidence = {
        group: [{"name": row.get("name"), "valuePerMatch": row.get("perMatch"), "isRate": "%" in str(row.get("name") or "") or "percentage" in str(row.get("name") or "").casefold()} for row in rows if allowed(row.get("name")) and valid_metric(str(row.get("name") or ""), row.get("perMatch"), nonzero=True)]
        for group, rows in team_metrics.items() if group in groups
    }
    team_evidence = {group: rows for group, rows in team_evidence.items() if rows}
    team_evidence.update(_team_defense_threat_evidence(reports, team_id))
    reports = _team_player_metric_reports(reports)
    players: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report.get("lineups") or []:
            if int(row.get("team_id") or 0) != int(team_id):
                continue
            position_name = str(row.get("position_name") or "").casefold()
            if "goalkeeper" in position_name or "kaleci" in position_name:
                continue
            impact = {str(item.get("name")): item.get("value") for item in (row.get("categories") or {}).get("contribution_impact", []) or []}
            try:
                minutes = float(impact.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            key = str(row.get("player_id") or row.get("player_name"))
            player = players.setdefault(key, {"playerId": row.get("player_id"), "name": row.get("player_name"), "imageUrl": row.get("player_image_url"), "position": row.get("position_name"), "minutes": 0.0, "metrics": defaultdict(lambda: defaultdict(list))})
            player["minutes"] += minutes
            for group in groups:
                for metric in (row.get("categories") or {}).get(group, []) or []:
                    name = str(metric.get("name") or "")
                    if not allowed(name):
                        continue
                    try:
                        player["metrics"][group][name].append((float(metric.get("value")), minutes))
                    except (TypeError, ValueError):
                        continue
    candidates = []
    for player in players.values():
        if player["minutes"] < 90:
            continue
        position_name = str(player.get("position") or "").casefold()
        if "goalkeeper" in position_name or "kaleci" in position_name:
            continue
        compact, score = {}, 0.0
        for group, metrics in player["metrics"].items():
            compact[group] = {}
            for name, samples in metrics.items():
                lower = name.casefold()
                is_rate = "%" in name or "percentage" in lower
                raw = sum(value * minutes for value, minutes in samples) / sum(minutes for _, minutes in samples) if is_rate else sum(value for value, _ in samples)
                value = raw if is_rate else raw / sum(minutes for _, minutes in samples) * 90
                if not valid_metric(name, round(value, 2), nonzero=True):
                    continue
                compact[group][name] = {"value": round(value, 2), "basis": "rate" if is_rate else "per90"}
                positive = any(word in lower for word in ("tackle", "interception", "clearance", "recovery", "duel", "aerial", "block", "header"))
                negative = any(word in lower for word in ("lost", "error", "foul", "card"))
                score += raw * (1 if positive else -.5 if negative else 0)
        if any(compact.values()):
            player.update({"minutes": round(player["minutes"]), "metrics": compact, "defenseScore": round(score, 2)})
            candidates.append(player)
    candidates.sort(key=lambda row: (-row["defenseScore"], -row["minutes"]))
    shortlist = candidates[:8]

    def cell(name: str, value: Any, basis: str) -> dict[str, str]:
        numeric = float(value)
        shown = str(int(numeric)) if numeric.is_integer() else f"{numeric:.2f}".rstrip("0").rstrip(".")
        return {"label": name, "value": f"%{shown}" if basis == "rate" else shown}

    if not include_analysis:
        from .team_profile_comparison import visual_profile
        return visual_profile(selected_styles, shortlist, cell, STYLE_VERSION, 'defence')

    flat_team = [(str(row["name"]), row["valuePerMatch"], "rate" if row["isRate"] else "perMatch") for rows in team_evidence.values() for row in rows]
    if not team_evidence and not selected_styles:
        return {"themes": [], "players": []}
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Report generation is not configured")
    try:
        parsed = generate_profile_json([
            ("system", "You are ScoutWise Enterprise's senior defensive football analyst. Return only valid JSON with exactly themes and players. Copy metric labels exactly only in metric-cell label fields; translate all metric references in narrative analysis into the requested language. Team volumes are per-match averages; player volumes are per 90. Never mix team and player values. Every analysis field MUST be a JSON array of complete strings, never one string. Never select a goalkeeper and never use saves or goalkeeper save data. Never write /90. The opponent_threat fields describe what past opponents produced against THIS team, not the team's own attacking output. Lower values in this group are preferable. Read all supplied threat indicators together with the existing defensive actions to assess exposure and resistance. Relate shot and chance volumes to xGA and xGoTA, and successful crosses and dribbles to their pooled success rates when available. Fewer defensive actions alone do not establish better defending; more actions alone do not establish superiority. Do not infer a particular pitch location, press, or defensive shape from these totals. These opponent-derived fields are team-only evidence; never assign them to individual players. Return exactly the supplied selected defensive styles in the supplied order (normally two). Copy styleId and title exactly; never invent, rename, replace or add a style. Selection uses the same deterministic statistical classifier as the coach report. Every theme must have styleId, title, three or four single-metric cells from its own source_metrics, evidence_metrics and an analysis array of exactly TWO professional bullets of 40-50 words each. Use only that style's source_metrics as its paragraph evidence. Keep the readingBoundary: these labels indicate statistical style fits, not directly observed pitch locations, marking assignments or pressing timing. Selected theme sources may include opponent offsides as contextual evidence; never attribute those to this team's own attacking output or to a player. Interpret the team's defensive identity with the same depth used in a professional attacking profile: connect the complete evidence across ball winning, interventions, duels, aerial control, recoveries, blocks, clearances, errors and discipline; lead with what the team repeatedly does, then explain security, exposure, efficiency or dependency. Discuss only the supplied defensive evidence positively and directly. Never explain that an excluded, unavailable or attacking metric is not a defensive measure; omit it silently and analyse what is present. Return exactly two key defensive outfield players copied from the shortlist, including exact playerId. Every player must have exactly FOUR single-metric cells selected independently for that player's own position and distinctive evidence. Avoid filling a card with variants of one family such as aerial total, aerial won and aerial lost; cover four different defensive facets whenever data permits. The two players must not automatically receive the same four metrics. Every player analysis must be a JSON array of exactly TWO player-specific bullets of 20-30 words each. Consider all supplied metrics internally, including metrics not displayed. Each bullet must lead with a football conclusion, contain at most one short numeric fact, and interpret relationships rather than define or list metrics. Do not invent pressing schemes, tactical intent, benchmarks or causation. Turkish analysis must use natural professional terminology. Keep metric-cell label identifiers unchanged for the UI to translate; never copy English metric identifiers into Turkish analysis strings. No markdown." + FOOTBALL_TERMINOLOGY_GUIDANCE + PROFILE_EVIDENCE_GUIDANCE.replace("defensive team themes\n  up to 2", "defensive team themes\n  up to 4")),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nTeam defensive metrics: {json.dumps(team_evidence, ensure_ascii=False)}\nSelected defensive styles with authoritative diagnostic cells: {json.dumps(selected_styles, ensure_ascii=False)}\nDefensive player shortlist: {json.dumps(shortlist, ensure_ascii=False, default=str)}"),
        ], require_players=True)
        team_source = {name: cell(name, value, basis) for name, value, basis in flat_team}
        player_sources = [{
            **{key: player[key] for key in ("playerId", "name", "imageUrl", "position", "minutes", "averageRating") if key in player},
            "source_metrics": {name: cell(name, detail["value"], detail["basis"]) for rows in player["metrics"].values() for name, detail in rows.items() if allowed(name)},
        } for player in shortlist]
        result = build_evidence_profile({**parsed, 'themes': []}, team_source, player_sources, theme_limit=4)
        result['themes'] = bind_selected_styles(parsed, selected_styles)
        result['styleVersion'] = STYLE_VERSION
        return result
    except Exception as exc:
        print("[enterprise_team_report] event=profile_generation_failed "
              f"section=build_team_report_defense_profile error_type={type(exc).__name__}")
        raise


def build_team_report_score_flow_profile(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en", include_ai: bool = True
) -> dict[str, Any]:
    """Aggregate time and outcome-changing events by level/ahead/behind game state."""
    state_order = ("ahead", "level", "behind")
    totals = {
        key: {"minutes": 0.0, "segments": 0, "matchesEntered": 0, "goalsFor": 0, "goalsAgainst": 0, "exitsPositive": 0, "exitsNegative": 0, "toAhead": 0, "toLevel": 0, "toBehind": 0, "matchesToAhead": 0, "matchesToLevel": 0, "matchesToBehind": 0, "finalWins": 0, "finalDraws": 0, "finalLosses": 0}
        for key in state_order
    }
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")

    def state(team_score: int, opponent_score: int) -> str:
        return "ahead" if team_score > opponent_score else "behind" if team_score < opponent_score else "level"

    seen_fixtures = set()
    for report in reports:
        fid = (report.get("fixture") or {}).get("id")
        if fid is not None and fid in seen_fixtures:
            continue
        if fid is not None:
            seen_fixtures.add(fid)
        teams = report.get("teams") or []
        analysed = next((team for team in teams if int(team.get("id") or 0) == int(team_id)), None)
        if not analysed:
            continue
        home = str(analysed.get("location") or "").casefold() == "home"
        fixture = report.get("fixture") or {}
        try:
            duration = max(90.0, float(fixture.get("length") or 90))
        except (TypeError, ValueError):
            duration = 90.0
        goals = []
        for event in report.get("events") or []:
            event_type = str(event.get("type") or "").casefold()
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
            goals.append({"minute": minute, "teamId": int(event.get("team_id") or 0), "result": result})
            duration = max(duration, minute)
        goals.sort(key=lambda item: item["minute"])
        team_score = opponent_score = 0
        cursor = 0.0
        transitions = set()
        entered = {"level"}
        current = "level"
        totals[current]["segments"] += 1
        for goal in goals:
            minute = min(duration, max(cursor, goal["minute"]))
            totals[current]["minutes"] += minute - cursor
            parsed_score = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", goal["result"])
            if parsed_score:
                home_score, away_score = int(parsed_score.group(1)), int(parsed_score.group(2))
                next_team_score, next_opponent_score = (home_score, away_score) if home else (away_score, home_score)
                scoring_for = next_team_score > team_score
                team_score, opponent_score = next_team_score, next_opponent_score
            else:
                scoring_for = goal["teamId"] == int(team_id)
            totals[current]["goalsFor" if scoring_for else "goalsAgainst"] += 1
            if not parsed_score and scoring_for:
                team_score += 1
            elif not parsed_score:
                opponent_score += 1
            next_state = state(team_score, opponent_score)
            if next_state != current:
                positive = (current == "level" and next_state == "ahead") or (current == "behind" and next_state == "level")
                totals[current]["exitsPositive" if positive else "exitsNegative"] += 1
                totals[current][f"to{next_state.title()}"] += 1
                transitions.add((current, next_state))
                totals[next_state]["segments"] += 1
                entered.add(next_state)
                current = next_state
            cursor = minute
        totals[current]["minutes"] += max(0.0, duration - cursor)
        for origin, target in transitions:
            totals[origin][f"matchesTo{target.title()}"] += 1
        for key in entered:
            totals[key]["matchesEntered"] += 1
            outcome_key = "finalWins" if team_score > opponent_score else "finalLosses" if team_score < opponent_score else "finalDraws"
            totals[key][outcome_key] += 1

    total_minutes = sum(row["minutes"] for row in totals.values()) or 1.0
    labels = {
        "level": "Beraberlikte" if lang == "tr" else "Level",
        "ahead": "Öndeyken" if lang == "tr" else "Ahead",
        "behind": "Gerideyken" if lang == "tr" else "Behind",
    }
    states = []
    for key in state_order:
        row = totals[key]
        states.append({
            "key": key, "label": labels[key], "minutes": round(row["minutes"], 1),
            "share": round(row["minutes"] / total_minutes * 100, 1),
            "averageSpell": round(row["minutes"] / row["segments"], 1) if row["segments"] else 0,
            "segments": row["segments"], "matchesEntered": row["matchesEntered"],
            "goalsFor": row["goalsFor"], "goalsAgainst": row["goalsAgainst"],
            "exitsPositive": row["exitsPositive"], "exitsNegative": row["exitsNegative"],
            "toAhead": row["toAhead"], "toLevel": row["toLevel"], "toBehind": row["toBehind"],
            "matchesToAhead": row["matchesToAhead"], "matchesToLevel": row["matchesToLevel"], "matchesToBehind": row["matchesToBehind"],
            "finalWins": row["finalWins"], "finalDraws": row["finalDraws"], "finalLosses": row["finalLosses"],
        })
    from match_analysis_module.score_flow_rates import rate_evidence, rate_perspectives
    fallback = rate_perspectives(states, lang)
    if not include_ai or not reports or not os.getenv("OPENAI_API_KEY"):
        return {"states": states, "perspectives": fallback}
    try:
        from langchain_openai import ChatOpenAI
        narrative_states = rate_evidence(states)
        response = ChatOpenAI(model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")), api_key=os.environ["OPENAI_API_KEY"], temperature=.2).invoke([
            ("system", "You are ScoutWise Enterprise's senior game-state analyst. Return only valid JSON with exactly level, ahead and behind. Write one concise 25-30 word professional interpretation for each state in the requested language. Prioritize time shares, distinct-match transition/outcome percentages and goals per 90 minutes spent specifically in that state. Each transition counts once per match; the denominator is matches entering the state. Transition percentages may overlap and need not sum to 100%. Winning after leading does not mean the lead was never lost. Cite at most two numerical facts. Make football inference dominant, not a statistical recap. Account for small match counts and avoid confident generalizations from few matches. Null is unavailable, never zero. Do not infer tactics or causation. No recommendations or markdown." + FOOTBALL_TERMINOLOGY_GUIDANCE),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nNormalized score-state evidence: {json.dumps(narrative_states, ensure_ascii=False)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        perspectives = {}
        for key in state_order:
            value = str(parsed.get(key) or "").strip()
            numeric_facts = re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?", value)
            perspectives[key] = value if value and len(numeric_facts) <= 2 else fallback[key]
        return {"states": states, "perspectives": perspectives}
    except Exception as exc:
        print(f"[enterprise_team_report] event=score_flow_profile_fallback error={exc}")
        return {"states": states, "perspectives": fallback}


def build_team_report_strengths(
    reports: list[dict[str, Any]], team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]], lang: str = "en",
    score_flow_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from report_module.team_findings import build_findings
    return build_findings(reports, team_id, 'strength', lang, score_flow_profile)


def build_team_report_weaknesses(
    reports: list[dict[str, Any]], team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]], strengths: dict[str, Any] | None = None,
    lang: str = "en", score_flow_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from report_module.team_findings import build_findings
    return build_findings(reports, team_id, 'weakness', lang, score_flow_profile, strengths)


def build_team_report_overview(
    reports: list[dict[str, Any]],
    team_id: int,
    team_perspectives: dict[str, str],
    player_perspectives: dict[str, dict[str, str]],
    momentum_perspectives: dict[str, str],
    regional_perspective: str,
    attack_profile: dict[str, Any],
    defense_profile: dict[str, Any],
    score_flow_profile: dict[str, Any],
    strengths: dict[str, Any],
    weaknesses: dict[str, Any],
    lang: str = "en",
) -> list[dict[str, Any]]:
    """Synthesize every team-report page into a compact section-by-section overview."""
    from report_module.profile_overview import CONTRACT, FIELDS, bind_row
    team = next((team for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), {})
    team_name = str(team.get("name") or ("Takım" if lang == "tr" else "The team"))
    formations = [str(team.get("formation") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id) and team.get("formation")]
    lineups = [row for report in reports for row in report.get("lineups") or [] if int(row.get("team_id") or 0) == int(team_id)]
    fixtures = [{"fixture": report.get("fixture"), "summary": report.get("summary")} for report in reports]
    section_titles = {
        "team_card": "Takım Kartı" if lang == "tr" else "Team Card",
        "form_results": "Form ve Sonuçlar" if lang == "tr" else "Form & Results",
        "squad_tactics": "Kadro Profili ve Taktik" if lang == "tr" else "Squad Profile & Tactics",
        "momentum": "Momentum" if lang == "tr" else "Momentum",
        "score_flow": "Skor Akış Profili" if lang == "tr" else "Score Flow Profile",
        "regional": "Bölgesel Oyun Dağılımı" if lang == "tr" else "Regional Play Distribution",
        "attack": "Hücum Profili" if lang == "tr" else "Attacking Profile",
        "defense": "Savunma Profili" if lang == "tr" else "Defensive Profile",
        "strengths": "Güçlü Yönler" if lang == "tr" else "Strengths",
        "weaknesses": "Zayıf Yönler" if lang == "tr" else "Weaknesses",
        "players": "Oyuncu Verileri" if lang == "tr" else "Player Data",
        "team_data": "Takım Verileri" if lang == "tr" else "Team Data",
    }
    evidence = {
        "team_card": {"team": team, "league": next((report.get("league") for report in reports if report.get("league")), None), "venue": next((report.get("venue") for report in reports if report.get("venue")), None)},
        "form_results": fixtures,
        "squad_tactics": {"formations": formations, "players_with_appearances": len({str(row.get("player_id") or row.get("player_name")) for row in lineups})},
        "momentum": momentum_perspectives,
        "score_flow": score_flow_profile,
        "regional": regional_perspective,
        "attack": attack_profile,
        "defense": defense_profile,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "players": player_perspectives,
        "team_data": team_perspectives,
    }
    fallback = [
        {"key": key, "category": title, "summary": (
            f"{team_name} için bu bölümdeki seçili maç verileri ve ScoutWise çıkarımları, takım profilinin ilgili performans boyutunu özetliyor."
            if lang == "tr" else
            f"The selected-match evidence and ScoutWise conclusions in this section summarize the relevant performance dimension of {team_name}."
        ), "subBullets": []}
        for key, title in section_titles.items()
    ]
    if not reports or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        # The modal and PDF already build these rows from structured evidence.
        keys = [key for key in section_titles if key not in {"momentum", "attack", "defense", "strengths", "weaknesses"}]
        visible_evidence = {key: evidence[key] for key in keys}
        response = ChatOpenAI(model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")), api_key=os.environ["OPENAI_API_KEY"], temperature=.2).invoke([
            ("system", "You are ScoutWise Enterprise's lead team-report editor. Return only one valid JSON object whose top-level keys exactly match REQUIRED_KEYS. Each value must contain exactly summary and subBullets. summary must be a polished 20-32 word synthesis of that report page's decisive football conclusion, not a description of what the page contains. subBullets must be an array of zero to two objects with exactly label and text; label is a concise 2-4 word insight name and text is an 8-16 word supporting conclusion. Synthesize the supplied existing evidence and ScoutWise interpretations without contradicting them. Cover the team's identity across every section, prioritize inference over facts, and avoid repeating the same conclusion across pages. Team Card may be factual. Form & Results summarizes outcome pattern; Squad Profile & Tactics summarizes usage and formation evidence; Score Flow summarizes level/ahead/behind control; Regional summarizes recurring spatial character; Player Data summarizes standout/development patterns without listing players unnecessarily; Team Data summarizes the broad statistical identity. Never mention missing data, fallback, API, match count, raw internal keys, unavailable evidence, recommendations or limitations. Do not invent tactics, causation or benchmarks. When the requested language is Turkish, use only natural Turkish football terminology except proper names and established metric abbreviations; never use foreign adjectives such as 'consequential'. No markdown." + CONTRACT + FOOTBALL_TERMINOLOGY_GUIDANCE),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nREQUIRED_KEYS: {json.dumps(keys)}\nSection titles: {json.dumps({key: section_titles[key] for key in keys}, ensure_ascii=False)}\nExisting report evidence: {json.dumps(visible_evidence, ensure_ascii=False, default=str)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if not isinstance(parsed, dict):
            raise ValueError("team overview response root was not a JSON object")
        rows = []
        for index, (key, title) in enumerate(section_titles.items()):
            if key not in keys:
                rows.append(fallback[index])
                continue
            item = parsed.get(key) if isinstance(parsed.get(key), dict) else {}
            def naturalize_overview(value: Any) -> str:
                text = str(value or "").strip()
                if lang == "tr":
                    replacements = {
                        r"\bconsequential\b": "belirleyici",
                        r"\bfriction\b|\bsürtünme\b": "verim kaybı",
                        r"\bdelivery\b|\bteslimat\b": "servis",
                    }
                    for pattern, replacement in replacements.items():
                        text = re.sub(pattern, replacement, text, flags=re.I)
                return text
            summary = naturalize_overview(item.get("summary") or "")
            bullets = [{"label": naturalize_overview(row.get("label") or ""), "text": naturalize_overview(row.get("text") or "")} for row in (item.get("subBullets") or [])[:(3 if key in {"strengths","weaknesses"} else 2)] if isinstance(row, dict) and str(row.get("text") or "").strip()]
            row = {"key": key, "category": title, "summary": summary or fallback[index]["summary"], "subBullets": bullets}
            if key in FIELDS:
                try:
                    row = bind_row(row, evidence[key])
                except ValueError:
                    pass  # A saved report can retry compact synthesis on reopening.
            rows.append(row)
        missing = [row["key"] for row, default in zip(rows, fallback) if row["key"] in keys and row["summary"] == default["summary"]]
        if missing:
            print(f"[enterprise_team_report] event=overview_partial_fallback missing_keys={missing}")
        return rows
    except Exception as exc:
        print(f"[enterprise_team_report] event=overview_fallback error={exc}")
        return fallback
