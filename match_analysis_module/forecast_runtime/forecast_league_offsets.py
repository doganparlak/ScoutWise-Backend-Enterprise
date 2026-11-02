"""Portable serving-only league-offset transforms, kept in parity with mobile."""
import math
import numpy as np

def apply_elo_gap(raw, names, home_gap):
    """Preserve pair mean and make same-league adjustment exactly zero.

    Gap is home-minus-away, one value per fixture; rows alternate home/away.
    Keeping the pair mean avoids injecting a league-quality intercept into
    same-league matches through separately fitted own/opponent Elo coefficients.
    """
    result = np.asarray(raw, dtype=float).copy()
    gaps = np.asarray(home_gap, dtype=float)
    if len(result) != 2 * len(gaps) or not np.all(np.isfinite(gaps)):
        raise ValueError('Expected one finite Elo gap per home/away row pair')
    rating, opponent = names.index('rating'), names.index('opponent_rating')
    delta = np.column_stack((gaps / 2., -gaps / 2.)).ravel()
    result[:, rating] += delta
    result[:, opponent] -= delta
    return result

def fixture_elo_gap(model, fixture):
    """Portable serving: numeric artifact only; no fitting or provider calls."""
    component = model.get('leagueOffsets')
    if not component:
        return 0.
    if component.get('kind') != 'residual_league_elo_offset_v1':
        raise ValueError('Unsupported league-offset artifact')
    if int(fixture['league']['id']) not in component['internationalCompetitionIds']:
        return 0.
    mapping = component['teamAssociations']
    home = mapping.get(str(fixture['homeTeam']['id']))
    away = mapping.get(str(fixture['awayTeam']['id']))
    offsets = component['offsets']
    if home not in offsets or away not in offsets:
        return 0.
    gap = float(offsets[home] - offsets[away])
    if not math.isfinite(gap): raise ValueError('Invalid league Elo gap')
    return gap

def adjust_elo_context(context, home_gap, home):
    half = home_gap / 2. if home else -home_gap / 2.
    return {**context, 'rating': context['rating'] + half,
            'opponent_rating': context['opponent_rating'] - half}
