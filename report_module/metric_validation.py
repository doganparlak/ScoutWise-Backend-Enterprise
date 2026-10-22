"""Validation shared by report calculations and selected player evidence."""
from math import isfinite
from typing import Any


def numeric(value: Any) -> float | None:
    try:
        result = float(str(value).strip().rstrip('%'))
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def is_percentage(name: str) -> bool:
    return '%' in name or 'percentage' in name.casefold()


def is_standard_count(name: str, group: str = "") -> bool:
    """Missing event counts are zero; advanced indices/rates remain unknown."""
    lower = name.casefold()
    return bool(name) and group != "expected" and not is_percentage(name) and not any(
        word in lower for word in ("expected", "performance", "rating", "captain", "minutes played")
    ) and not lower.startswith(("xg", "xa", "npxg"))


def has_team_statistics(categories: dict[str, list[dict[str, Any]]]) -> bool:
    """Require a recorded standard statistic before interpreting omissions as zero.

    Advanced-only feeds and previously inferred zeros do not establish coverage.
    An explicitly recorded zero is a valid statistic.
    """
    return any(
        not row.get("_inferred_zero")
        and valid_metric(str(row.get("name") or ""), row.get("value"))
        and (is_standard_count(str(row.get("name") or ""), group)
             or (group != "expected" and is_percentage(str(row.get("name") or ""))))
        for group, rows in categories.items() for row in rows
    )


def zero_fill_counts(categories: dict[str, list[dict[str, Any]]],
                     names: set[tuple[str, str]]) -> None:
    """Fill omitted counts only when usable standard team statistics exist."""
    if not has_team_statistics(categories):
        return
    for group, name in sorted(names):
        if not is_standard_count(name, group):
            continue
        rows = categories.setdefault(group, [])
        existing = next((row for row in rows if row.get("name") == name), None)
        if existing is None:
            rows.append({"name": name, "value": 0.0, "_inferred_zero": True})
        elif numeric(existing.get("value")) is None:
            existing["value"] = 0.0


def valid_metric(name: str, value: Any, *, nonzero: bool = False) -> bool:
    number = numeric(value)
    return number is not None and (not nonzero or number != 0) and (
        not is_percentage(name) or 0 <= number <= 100
    )


def percentage(numerator: Any, denominator: Any) -> float | None:
    a, b = numeric(numerator), numeric(denominator)
    if a is None or b is None or b <= 0 or not 0 <= a <= b:
        return None
    return a / b * 100


def rate_counts(name: str, counts: dict[str, Any], numerator: str,
                denominator: str) -> tuple[float, float] | None:
    a, b = numeric(counts.get(numerator)), numeric(counts.get(denominator))
    if name in {'Aerials Won (%)', 'Aerials Won Percentage'} and a is not None and 'Aerials Lost' in counts:
        lost = numeric(counts['Aerials Lost'])
        if lost is None or lost < 0:
            return None
        b = a + lost
    return (a, b) if percentage(a, b) is not None else None


def nonzero_evidence(groups: dict[str, Any]) -> dict[str, Any]:
    """Filter selected-player evidence, preserving the raw statistical tables."""
    return {group: {name: value for name, value in metrics.items()
                    if valid_metric(name, value, nonzero=True)}
            for group, metrics in groups.items()}


def nonzero_metric_rows(groups: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Copy non-zero metric rows for interpretation, preserving raw calculations."""
    result = {}
    for group, rows in groups.items():
        kept = [dict(row) for row in rows or [] if isinstance(row, dict)
                and valid_metric(str(row.get("name") or ""), row.get("value"), nonzero=True)]
        if kept:
            result[group] = kept
    return result


def nonzero_report_metric_evidence(content: Any) -> Any:
    """Filter metric collections in narrative snapshots; keep result/event context."""
    if isinstance(content, list):
        return [nonzero_report_metric_evidence(item) for item in content]
    if not isinstance(content, dict):
        return content
    result = {}
    for key, value in content.items():
        if key in {"categories", "team_comparison"} and isinstance(value, dict):
            result[key] = nonzero_metric_rows(value)
        elif key in {"expected_metrics", "per90_metrics", "standout_metrics", "development_metrics"}:
            if isinstance(value, list):
                result[key] = nonzero_metric_rows({key: value}).get(key, [])
            elif isinstance(value, dict):
                result[key] = nonzero_evidence({key: value})[key]
            else:
                result[key] = value
        else:
            result[key] = nonzero_report_metric_evidence(value)
    return result


def sanitize_percentages(content: Any) -> Any:
    """Remove invalid percentage values from structured report snapshots."""
    if isinstance(content, list):
        kept = []
        for item in content:
            if isinstance(item, dict):
                name = str(item.get('name') or item.get('metric') or item.get('stat') or item.get('label') or '')
                value = item.get('value', item.get('data'))
                if isinstance(value, dict):
                    value = value.get('value')
                if is_percentage(name) and not valid_metric(name, value):
                    continue
            kept.append(sanitize_percentages(item))
        return kept
    if isinstance(content, dict):
        return {key: sanitize_percentages(value) for key, value in content.items()
                if not is_percentage(str(key)) or valid_metric(
                    str(key), value.get("value") if isinstance(value, dict) and "value" in value else value)}
    return content


def sanitize_player_highlights(content: Any) -> Any:
    """Apply highlight rules to saved snapshots without changing source data."""
    if isinstance(content, list):
        return [sanitize_player_highlights(item) for item in content]
    if not isinstance(content, dict):
        return content
    result = {key: sanitize_player_highlights(value) for key, value in content.items()}
    for key in ('standout_metrics', 'development_metrics'):
        if isinstance(result.get(key), list):
            result[key] = [metric for metric in result[key] if isinstance(metric, dict)
                           and valid_metric(str(metric.get('name') or ''), metric.get('value'), nonzero=True)
                           and (not metric.get('is_percentage') or percentage(metric.get('value'), 100) is not None)]
    return result
