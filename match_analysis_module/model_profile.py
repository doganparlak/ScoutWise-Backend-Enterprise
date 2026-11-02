"""Read the shared approved ScoutWise model for Enterprise pre-match reports.

No fitting, activation, schema writes or Sportmonks calls occur on this path.
Saved report profiles remain immutable; older reports can be enriched on read.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import logging
import math
from threading import RLock

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api_module.database import engine
from .forecast_runtime import forecast as f

_LOG = logging.getLogger(__name__)
_CACHE = OrderedDict()
_LOCK = RLock()


def _artifact(key, load):
    with _LOCK:
        value = _CACHE.get(key)
        if value is None:
            value = load()
            _CACHE[key] = value
            while len(_CACHE) > 7:
                _CACHE.popitem(last=False)
        _CACHE.move_to_end(key)
        return value


def _load_model(cutoff):
    with engine.connect() as db:
        model_id = db.execute(text("""SELECT id FROM mobile_match_forecast_models
            WHERE model_version IN (6,5) AND training_cutoff<:cutoff
              AND validation_metrics->>'promotionApproved'='true'
            ORDER BY is_active DESC,training_cutoff DESC LIMIT 1"""), {'cutoff': cutoff}).scalar()
        if model_id is None:
            return None
        model = _artifact(('model', model_id), lambda: {
            **db.execute(text('SELECT artifact FROM mobile_match_forecast_models WHERE id=:id'),
                         {'id': model_id}).scalar_one(), 'modelId': model_id})
    # Daily histories are produced by mobile's existing scheduled job. Enterprise
    # consumes the same settled snapshot strictly before the prediction cutoff.
    try:
        with engine.connect() as db:
            stamp = db.execute(text('''SELECT as_of FROM mobile_match_forecast_histories
                WHERE model_id=:id AND as_of<:cutoff ORDER BY as_of DESC LIMIT 1'''),
                {'id': model_id, 'cutoff': cutoff}).scalar()
            if stamp is not None:
                history = _artifact(('history', model_id, stamp), lambda: db.execute(text('''
                    SELECT payload FROM mobile_match_forecast_histories
                    WHERE model_id=:id AND as_of=:stamp'''), {'id': model_id, 'stamp': stamp}).scalar_one())
                model = {**model, **history}
    except SQLAlchemyError:
        _LOG.warning('Enterprise model profile: daily snapshot unavailable; using trained history')
    if model.get('version') not in (5,6) or model.get('featureSchema') != 3:
        return None
    return model


def _observed(history):
    rows = history[-f.WINDOW:]
    result = {'matches': len(rows)}
    for field, key in (('goalsFor', 'gf'), ('goalsAgainst', 'ga')):
        result[field] = sum(row[key] for row in rows) / len(rows) if rows else None
    for field, key in (('xg', 'Expected Goals'), ('shotsOnTarget', 'Shots On Target')):
        available = [float(row.get('own', {}).get(key)) for row in rows
                     if row.get('own', {}).get(key) is not None
                     and math.isfinite(float(row['own'][key]))]
        result[field] = sum(available) / len(available) if len(available) >= max(3, math.ceil(len(rows) * .6)) else None
    return result


def build_model_profile(fixture, now=None):
    try:
        kickoff = f.utc(fixture['startingAt'])
        cutoff = min(now or datetime.now(timezone.utc), kickoff)
        model = _load_model(cutoff)
        if not model:
            return {'status': 'unavailable', 'reason': 'model_unavailable'}
        result = f.build_forecast(fixture, cutoff, model)
        if result['status'] != 'ready':
            return result
        forecast = result['forecast']
        home, away = forecast['home'], forecast['away']
        own, opponent = model['histories'][str(home['id'])], model['histories'][str(away['id'])]
        halves = None
        half_model = model.get('halfTimeModel')
        if half_model:
            totals = {int(key): value for key, value in half_model['leagueTotals'].items()}
            league = fixture['league']['id']
            shares = [f._half_share(own, opponent, f._half_prior(totals, league, True), half_model['priorStrength']),
                      f._half_share(opponent, own, f._half_prior(totals, league, False), half_model['priorStrength'])]
            halves = {side: {'home': home['projectedGoals'] * (shares[0] if side == 'first' else 1 - shares[0]),
                             'away': away['projectedGoals'] * (shares[1] if side == 'first' else 1 - shares[1])}
                      for side in ('first', 'second')}
        return {'status': 'ready', 'modelId': model['modelId'], 'modelVersion': model.get('version', 5),
                'asOf': forecast['asOf'], 'generatedAt': forecast['generatedAt'],
                'home': {**{key: home.get(key) for key in ('id', 'name', 'imageUrl', 'projectedGoals')},
                         'observed': _observed(own)},
                'away': {**{key: away.get(key) for key in ('id', 'name', 'imageUrl', 'projectedGoals')},
                         'observed': _observed(opponent)},
                'outcomes': forecast['outcomes'], 'goalDistribution': forecast['goalDistribution'],
                'scoreMargins': forecast['scoreMargins'],
                'grid': forecast['grid'], 'gridRemainder': forecast['gridRemainder'],
                'moreGoalsHalf': (forecast.get('halfScenarios') or {}).get('moreGoalsHalf'),
                'halves': halves}
    except Exception as exc:
        # A forecast outage never prevents the rest of a report from opening.
        _LOG.warning('Enterprise model profile unavailable (%s)', type(exc).__name__)
        return {'status': 'unavailable', 'reason': 'data_unavailable'}


def _extend_saved_profile(profile):
    """Add summaries from saved rates, preserving the original forecast."""
    if profile.get('status') != 'ready' or (profile.get('scoreMargins') and profile.get('grid') is not None and profile.get('gridRemainder') is not None):
        return profile
    try:
        with engine.connect() as db:
            row = db.execute(text("""SELECT artifact->>'rho' AS rho
                FROM mobile_match_forecast_models WHERE id=:id AND model_version=:version"""),
                {'id': profile.get('modelId'), 'version': profile.get('modelVersion')}).mappings().first()
        if row is None:
            return profile
        rho = float(row['rho'] or 0.)
        home, away = profile['home']['projectedGoals'], profile['away']['projectedGoals']
        distribution = f.score_distribution(home, away, rho)
        if not all(math.isclose(value, profile['outcomes'][key], abs_tol=1e-9)
                   for key, value in distribution['outcomes'].items()):
            return profile
        result = {**profile, 'scoreMargins': distribution['scoreMargins'],
                  'grid': distribution['grid'], 'gridRemainder': distribution['gridRemainder']}
        halves = profile.get('halves')
        if halves and not profile.get('moreGoalsHalf'):
            first = halves['first']['home'] + halves['first']['away']
            second = halves['second']['home'] + halves['second']['away']
            comparison = f.score_distribution(first, second)['outcomes']
            if rho:
                from .forecast_runtime.forecast_learning import bounded_rho
                hs, aws = halves['first']['home'] / home, halves['first']['away'] / away
                delta = math.exp(-home-away) * home * away * bounded_rho(home, away, rho)
                comparison['home'] += delta * (hs + aws - hs * aws)
                comparison['away'] += delta * (1 - hs * aws)
                comparison['draw'] -= delta * (1 + hs + aws - 2 * hs * aws)
            result['moreGoalsHalf'] = {'first': comparison['home'], 'equal': comparison['draw'], 'second': comparison['away']}
        return result
    except Exception as exc:
        _LOG.warning('Saved model profile summaries unavailable (%s)', type(exc).__name__)
        return profile


def enrich_model_profile(content, fixture):
    if content.get('report_type') != 'pre_match':
        return content
    profile = content.get('model_match_profile')
    if f.utc(fixture['startingAt']) > datetime.now(timezone.utc):
        return {**content, 'model_match_profile': build_model_profile(fixture)}
    if not profile:
        return {**content, 'model_match_profile': build_model_profile(fixture)}
    extended = _extend_saved_profile(profile)
    return content if extended is profile else {**content, 'model_match_profile': extended}
