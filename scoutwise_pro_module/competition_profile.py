"""Shared player_comp_data profiles for assistant fit assessments."""
import math
from collections import defaultdict
from fastapi import HTTPException
from sqlalchemy import text
from report_module.rate_evidence import apply_merged_rates


def competition_profile(rows):
    """Merge team records within one competition, weighted by appearances."""
    first = rows[0]
    result = {key: first.get(key) for key in
              ('player_id', 'player_name', 'team_name', 'league_name', 'age', 'height', 'weight')}
    result['match_count'] = sum(float(row.get('match_count') or 0) for row in rows)
    totals, weights, positions = defaultdict(float), defaultdict(float), defaultdict(float)
    for row in rows:
        weight = float(row.get('match_count') or 0)
        for role, count in (row.get('position_counts') or {}).items():
            positions[role] += float(count or 0)
        for metric, value in (row.get('stats') or {}).items():
            try:
                numeric = float(value)
            except (ValueError, TypeError):
                continue
            if weight > 0 and math.isfinite(numeric):
                totals[metric] += numeric * weight
                weights[metric] += weight
    result.update({key: totals[key] / weights[key] for key in totals})
    result['position_counts'] = dict(positions)
    apply_merged_rates(result, [row.get('stats') or {} for row in rows],
                       [float(row.get('match_count') or 0) for row in rows])
    return result


def load_competition_candidate(db, metadata, league_id, league_sub_type):
    candidate_rows = db.execute(text("""SELECT * FROM player_comp_data
        WHERE player_id = :id AND league_sub_type = :subtype"""),
        {'id': metadata.get('player_id'), 'subtype': league_sub_type}).mappings().all()
    if not candidate_rows:
        raise HTTPException(status_code=422, detail='Player competition data unavailable')
    # Prefer this competition when already present; otherwise the player's current
    # team's main competition of the same type. Never blend cup and league records.
    competitions = defaultdict(list)
    for row in candidate_rows:
        competitions[row['league_id']].append(row)
    def priority(item):
        competition_id, rows = item
        return (competition_id == league_id,
                any(str(row.get('team_id')) == str(metadata.get('team_id')) for row in rows),
                sum(float(row.get('match_count') or 0) for row in rows), -int(competition_id))
    _, selected = max(competitions.items(), key=priority)
    return competition_profile(selected)
