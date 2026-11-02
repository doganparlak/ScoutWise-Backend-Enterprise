"""Serving-only v5 transforms; keep numerical parity with mobile runtime."""
from datetime import datetime, timezone
import math

WINDOW = 10
MIN_HISTORY = 5

def utc(value):
    date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)


def _half_prior(totals, league, home):
    full, first = (1, 2) if home else (3, 4)
    global_full = sum(row[full] for row in totals.values())
    global_first = sum(row[first] for row in totals.values())
    pooled = (global_first + 1) / (global_full + 2)
    row = totals.get(league, [0, 0, 0, 0, 0])
    return (row[first] + 5 * pooled) / (row[full] + 5)


def _half_evidence(history, conceded=False):
    rows = history[-WINDOW:]
    first, full = ('hga', 'ga') if conceded else ('hgf', 'gf')
    available = [row for row in rows if row.get(first) is not None]
    if len(available) < max(3, math.ceil(len(rows) * .6)):
        return 0, 0
    return sum(row[first] for row in available), sum(row[full] for row in available)


def _half_share(own, opponent, prior, strength):
    if strength is None:
        return prior  # A learned league-only candidate, not a fixed 50/50 split.
    scored_first, scored_full = _half_evidence(own)
    conceded_first, conceded_full = _half_evidence(opponent, conceded=True)
    return (scored_first + conceded_first + strength * prior) / (scored_full + conceded_full + strength)


def half_scenarios(home_rate, away_rate, own, opponent, league, model, rho=0.):
    if not model:
        return None
    totals = {int(key): value for key, value in model['leagueTotals'].items()}
    home_share = _half_share(own, opponent, _half_prior(totals, league, True), model['priorStrength'])
    away_share = _half_share(opponent, own, _half_prior(totals, league, False), model['priorStrength'])
    first_home, first_away = home_rate * home_share, away_rate * away_share
    comparison = score_distribution(first_home + first_away, home_rate + away_rate - first_home - first_away)['outcomes']
    first_btts = (1 - math.exp(-first_home)) * (1 - math.exp(-first_away))
    if rho:
        # Thin the same corrected full-match joint distribution into halves.
        # Only the four Dixon-Coles cells change; this exact adjustment avoids
        # expensive goal-by-goal simulations and keeps every scenario coherent.
        from .forecast_learning import bounded_rho
        delta = math.exp(-home_rate - away_rate) * home_rate * away_rate * bounded_rho(home_rate, away_rate, rho)
        comparison['home'] += delta * (home_share + away_share - home_share * away_share)
        comparison['away'] += delta * (1 - home_share * away_share)
        comparison['draw'] -= delta * (1 + home_share + away_share - 2 * home_share * away_share)
        first_btts -= delta * home_share * away_share
    return {'moreGoalsHalf': {'first': comparison['home'], 'equal': comparison['draw'], 'second': comparison['away']},
            'firstHalfBothTeamsScore': first_btts}


def _context(totals, ratings, league, team, opponent):
    count, hg, ag = totals.get(league, [0, 0., 0.])
    all_count = sum(v[0] for v in totals.values())
    global_home = sum(v[1] for v in totals.values()) / all_count if all_count else 1.4
    global_away = sum(v[2] for v in totals.values()) / all_count if all_count else 1.1
    return {'league_home_goals': (hg + 5 * global_home) / (count + 5),
            'league_away_goals': (ag + 5 * global_away) / (count + 5),
            'rating': ratings.get(team, 1500.), 'opponent_rating': ratings.get(opponent, 1500.)}


def score_distribution(home_rate, away_rate, rho=0.):
    # Adaptive tail makes the visible grid a subset of a complete distribution.
    def goals(rate):
        probabilities = [math.exp(-rate)]
        k = 0
        while 1 - sum(probabilities) > 1e-12:
            k += 1
            probabilities.append(probabilities[-1] * rate / k)
            if k > 100:
                raise ValueError('Invalid goal distribution')
        return probabilities
    home, away = goals(home_rate), goals(away_rate)
    total = sum(home) * sum(away)
    from .forecast_learning import dc_factor
    scores = [{'home': h, 'away': a, 'probability': p * q * dc_factor(home_rate, away_rate, h, a, rho) / total}
              for h, p in enumerate(home) for a, q in enumerate(away)]
    return {'outcomes': {'home': sum(r['probability'] for r in scores if r['home'] > r['away']),
                         'draw': sum(r['probability'] for r in scores if r['home'] == r['away']),
                         'away': sum(r['probability'] for r in scores if r['home'] < r['away'])},
            # Aggregate the complete adaptive score distribution, including
            # scores beyond the visible 0-5 grid. No new model is fitted.
            'scoreMargins': {
                'homeOne': sum(r['probability'] for r in scores if r['home'] - r['away'] == 1),
                'homeTwoPlus': sum(r['probability'] for r in scores if r['home'] - r['away'] >= 2),
                'draw': sum(r['probability'] for r in scores if r['home'] == r['away']),
                'awayOne': sum(r['probability'] for r in scores if r['away'] - r['home'] == 1),
                'awayTwoPlus': sum(r['probability'] for r in scores if r['away'] - r['home'] >= 2)},
            'bothTeamsScore': sum(r['probability'] for r in scores if r['home'] and r['away']),
            'cleanSheet': {'home': sum(r['probability'] for r in scores if r['away'] == 0),
                           'away': sum(r['probability'] for r in scores if r['home'] == 0)},
            'over25': sum(r['probability'] for r in scores if r['home'] + r['away'] >= 3),
            'totalGoalRanges': [sum(r['probability'] for r in scores if r['home'] + r['away'] <= 1),
                                sum(r['probability'] for r in scores if 2 <= r['home'] + r['away'] <= 3),
                                sum(r['probability'] for r in scores if r['home'] + r['away'] >= 4)],
            'topScores': sorted(scores, key=lambda r: r['probability'], reverse=True)[:3],
            'grid': [r for r in scores if r['home'] <= 5 and r['away'] <= 5],
            'gridRemainder': sum(r['probability'] for r in scores if r['home'] > 5 or r['away'] > 5),
            'goalDistribution': {'home': home[:6] + [max(0., 1 - sum(home[:6]))],
                                  'away': away[:6] + [max(0., 1 - sum(away[:6]))]}}


def forecast_coverage_reason(fixture, cutoff, model):
    """Cheap shared eligibility check; no fitting, provider calls or prediction."""
    if not model or (model.get('asOf') and utc(model['asOf']) >= cutoff) or (model.get('historyAsOf') and utc(model['historyAsOf']) >= cutoff):
        return 'model_unavailable'
    season = str((fixture.get('season') or {}).get('name') or '').replace(' ', '').replace('/', '')
    trained_seasons = {str(c['seasonName']).replace(' ', '').replace('/', '') for c in model.get('competitions', [])}
    if season and season not in trained_seasons:
        return 'season_mismatch'
    own = model['histories'].get(str(fixture['homeTeam']['id']), [])
    opponent = model['histories'].get(str(fixture['awayTeam']['id']), [])
    if min(len(own), len(opponent)) < MIN_HISTORY:
        return 'insufficient_history'
    if any(utc(row['at']) >= cutoff for row in own + opponent):
        return 'history_after_cutoff'
    return None


def build_forecast(fixture, cutoff, model):
    reason = forecast_coverage_reason(fixture, cutoff, model)
    if reason == 'history_after_cutoff':
        raise ValueError('Forecast history crosses kickoff')
    if reason:
        return {'status': 'unavailable', 'reason': reason}
    home, away = fixture['homeTeam'], fixture['awayTeam']
    own, opponent = model['histories'].get(str(home['id']), []), model['histories'].get(str(away['id']), [])
    totals = {int(k): v for k, v in model['leagueTotals'].items()}
    ratings = {int(k): v for k, v in model['ratings'].items()}
    league = fixture['league']['id']
    a = _rate(model, own, opponent, True, {**_context(totals, ratings, league, home['id'], away['id']), 'kickoff': fixture['startingAt']})
    b = _rate(model, opponent, own, False, {**_context(totals, ratings, league, away['id'], home['id']), 'kickoff': fixture['startingAt']})
    if model.get('refinedEnsemble'):
        from .forecast_refined import predict_rates
        a, b = (float(value) for value in predict_rates(model, fixture, cutoff, (a, b)))
    return {'status': 'ready', 'forecast': {'fixtureId': fixture['fixtureId'], 'modelVersion': model.get('version', 2),
            'startingAt': fixture['startingAt'], 'asOf': model.get('historyAsOf') or model.get('asOf', cutoff.isoformat()), 'generatedAt': datetime.now(timezone.utc).isoformat(),
            'scope': model.get('scope', 'current_season_last_10'), 'modelId': model.get('modelId'), 'trainingCompetitions': model.get('competitions', []), 'regulationMinutes': 90,
            'home': {'id': home['id'], 'name': home['name'], 'imageUrl': home.get('imageUrl'), 'matches': len(own), 'projectedGoals': a},
            'away': {'id': away['id'], 'name': away['name'], 'imageUrl': away.get('imageUrl'), 'matches': len(opponent), 'projectedGoals': b},
            'trainingMatches': model['trainingMatches'], 'validationMatches': model['validationMatches'],
            'usesTeamStats': model['usesTeamStats'], 'features': model['features'],
            'halfScenarios': half_scenarios(a, b, own, opponent, league, model.get('halfTimeModel'), model.get('rho', 0.)),
            **score_distribution(a, b, model.get('rho', 0.))}}

def _rate(model, own, opponent, home, context=None):
    from .forecast_learning import rate
    return rate(model, own, opponent, home, context or {})
