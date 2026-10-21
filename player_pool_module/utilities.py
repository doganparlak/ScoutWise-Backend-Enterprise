from __future__ import annotations


FOLD_CHAR_MAP_FROM = (
    "çğıöşüÇĞİÖŞÜIİı"
    "áàâäãåāăąÁÀÂÄÃÅĀĂĄ"
    "éèêëēĕėęěÉÈÊËĒĔĖĘĚ"
    "íìîïīĭįİÍÌÎÏĪĬĮ"
    "óòôöõøōŏőÓÒÔÖÕØŌŎŐ"
    "úùûüūŭůűųÚÙÛÜŪŬŮŰŲ"
    "ñÑćčĆČłŁńŃřŘśšŚŠýÿÝŸžźżŽŹŻ"
)
FOLD_CHAR_MAP_TO = (
    "cgiosuCGIOSUiii"
    "aaaaaaaaaAAAAAAAAA"
    "eeeeeeeeeEEEEEEEEE"
    "iiiiiiiiIIIIIII"
    "oooooooooOOOOOOOOO"
    "uuuuuuuuuUUUUUUUUU"
    "nNccCClLnNrRssSSyyYYzzzZZZ"
)


def clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def norm_name(value: str | None) -> str:
    if not value:
        return ""
    translated = value.translate(str.maketrans(FOLD_CHAR_MAP_FROM, FOLD_CHAR_MAP_TO))
    return " ".join(translated.lower().split())


def player_name_pattern(value: str | None) -> str | None:
    """Match entered name fragments in order, allowing omitted middle names."""
    parts = (value or "").split()
    if not parts:
        return None
    # Only our separators are wildcards; user-entered %, _ and backslashes
    # must remain literal characters in PostgreSQL LIKE/ILIKE patterns.
    escaped = [part.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for part in parts]
    return "%" + "%".join(escaped) + "%"


def player_name_matches(value: str | None, query: str | None) -> bool:
    """Compare name fragments in order without requiring adjacent words."""
    candidate = norm_name(value)
    parts = norm_name(query).split()
    if not parts:
        return False
    start = 0
    for part in parts:
        index = candidate.find(part, start)
        if index < 0:
            return False
        start = index + len(part)
    return True


def player_pool_table(world_cup_mode: bool = False) -> str:
    return "player_data_wc" if world_cup_mode else "player_data"


def numeric_filter_sql(field_name: str, param_name: str, operator: str) -> str:
    value_expr = f"""
    CASE
        WHEN COALESCE(metadata->>'{field_name}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN (metadata->>'{field_name}')::numeric
        ELSE NULL
    END
    """
    return f"(:{param_name} IS NULL OR ({value_expr}) {operator} :{param_name})"


def folded_text_sql(field_name: str) -> str:
    return (
        "LOWER(TRANSLATE("
        f"COALESCE(metadata->>'{field_name}', ''), "
        f"'{FOLD_CHAR_MAP_FROM}', '{FOLD_CHAR_MAP_TO}'"
        "))"
    )
