"""Keep generated metric selections bound to their own source labels and values."""
from typing import Any


def bind_metric_cells(
    selected: Any,
    evidence: dict[str, dict[str, str]],
    limit: int,
) -> list[dict[str, str]]:
    if not isinstance(selected, list):
        return []
    result = []
    seen = set()
    for item in selected[:limit]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if label in evidence and label not in seen:
            result.append(dict(evidence[label]))
            seen.add(label)
    return result
