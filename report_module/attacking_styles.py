"""Use the coach classifier over the team's original recorded match snapshots."""
from copy import deepcopy
from math import ceil, isfinite, sqrt
from statistics import mean
from itertools import combinations, product

STYLE_VERSION = 3
DEFENCE_STYLE_VERSION = 2


def numeric(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _metric_options(style, catalog, side):
    from coach_pool_module.archetypes import BUNDLES
    signals = sorted(style['signals'],key=lambda signal:-(signal['weight']*signal['support']*signal['coverage']))
    strengths = {signal['metric']:signal['weight']*signal['support']*signal['coverage'] for signal in signals}
    core = {signal['metric'] for signal in style.get('distinctiveSignals') or []}
    if not core:
        core = {signal['metric'] for signal in signals[:2]}
    keys = list(dict.fromkeys([signal['metric'] for signal in signals] + BUNDLES[side][style['id']][0]))
    keys = [key for key in keys if key in catalog]
    options = []
    for chosen in combinations(keys,4):
        # Provider team totals and their outfield fallback cannot fill two cells.
        if not core.issubset(chosen) or len({key.removeprefix('Outfield ') for key in chosen})!=4:
            continue
        quality = sum(strengths.get(key,0) for key in chosen)
        options.append((chosen,quality))
    if not options:
        return []
    best = max(quality for _,quality in options)
    # Distinctness must not displace the defining evidence with weak filler.
    return sorted((option for option in options if option[1]>=best*.9),key=lambda option:(-option[1],option[0]))


def _four_metric_cells(keys,catalog):
    source = {}
    for key in keys:
        metric = catalog[key]
        value = metric['seasons'][0]['value']
        shown = f'{value:.2f}'.rstrip('0').rstrip('.')
        basis = 'rate' if metric['percentage'] else 'per100OpponentPasses' if 'Per 100 Opponent Passes' in key else 'per100Passes' if key.endswith(' Per 100 Passes') else 'perMatch'
        source[metric['name']] = {'label':metric['name'],'value':f'%{shown}' if metric['percentage'] else shown,
                                  'contributes':True,'sourceKey':key,'basis':basis,
                                  'matchesCovered':metric['seasons'][0]['covered']}
    return source


def _distinct_metric_sets(options):
    def objective(pair):
        sets = [{key.removeprefix('Outfield ') for key in keys} for keys,_ in pair]
        repeated = sum(len(a & b) for i,a in enumerate(sets) for b in sets[i+1:])
        return repeated,-sum(quality for _,quality in pair),tuple(tuple(keys) for keys,_ in pair)
    return min(product(*options),key=objective)


def select_team_styles(reports, team_id, lang='en', side='attack', single_match=False):
    if side not in {'attack', 'defence'}:
        raise ValueError('Unknown profile side')
    from coach_pool_module import profiles as evidence, tactics
    from coach_pool_module.archetypes import ATTACK, DEFENCE, DESCRIPTIONS
    registry = ATTACK if side == 'attack' else DEFENCE
    from coach_pool_module.classification import rank_styles, distinct_attack_candidate
    meta, samples, seen = {}, [], set()
    players = evidence.PlayerSamples(team_id, meta)
    for index, report in enumerate(reports):
        fid = (report.get('fixture') or {}).get('id') or f'snapshot:{index}'
        if fid in seen:
            continue
        seen.add(fid)
        teams = report.get('teams') or []
        if not any(int(team.get('id') or 0) == team_id for team in teams):
            continue
        stats = []
        for team in teams:
            sources = {**(team.get('categories') or {}), 'expected': team.get('expected_metrics') or []}
            for rows in sources.values():
                for row in rows:
                    if not row.get('_inferred_zero') and numeric(row.get('value')) is not None:
                        stats.append({'participant_id':team['id'], 'type':{'name':row.get('name')}, 'data':{'value':row['value']}})
        sample = evidence.extract_match({'participants':[{'id':team['id']} for team in teams], 'statistics':stats}, team_id, meta)
        outfield = []
        for row in report.get('lineups') or []:
            if int(row.get('team_id') or 0) != team_id:
                continue
            values = {metric['name']:numeric(metric.get('value')) for rows in (row.get('categories') or {}).values()
                      for metric in rows if metric.get('name') and not metric.get('_inferred_zero')}
            minutes = values.get('Minutes Played')
            if minutes is None or minutes <= 0:
                continue
            player = {'id':row.get('player_id') or row.get('player_name'), 'teamId':team_id,
                      'name':row.get('player_name') or '', 'position':row.get('position_name') or '',
                      'positionId':row.get('position_id'), 'minutes':minutes,
                      'metrics':{name:value for name,value in values.items() if name in evidence.PLAYER_METRICS and value is not None and value >= 0}}
            if tactics.role_name(player) != 'goalkeeper':
                outfield.append(player)
        players.add({'id':fid, 'players':outfield})
        sample.update(players.samples[fid])
        evidence.add_context_rates(sample, meta)
        samples.append(sample)
    if not samples:
        return []
    catalog = {}
    for key, definition in meta.items():
        if evidence.FORBIDDEN.search(key):
            continue
        observed = [s for s in samples if key in s]
        if len(observed) < max(min(5, len(samples)), ceil(len(samples) * .7)):
            continue
        value = evidence.pool(observed, [key], meta)[0]['value']
        values = [s[key][0] / s[key][1] * (100 if definition['percentage'] and key != 'Ball Possession %' else 1) for s in observed]
        average = mean(values)
        variation = sqrt(mean((v-average)**2 for v in values)) / max(abs(average), .001)
        catalog[key] = {**definition, 'seasons':[{'value':value, 'covered':len(observed), 'matches':len(samples), 'matchVariation':variation,'matchValues':values}]}
    selected, selected_fits, metric_options = [], [], []
    for style in rank_styles(catalog, side, secondary_match=single_match):
        if style['coverage'] <= 0 or style['statisticalFit'] <= 0:
            continue
        if side == 'attack' and not distinct_attack_candidate(style, selected_fits):
            continue
        options = _metric_options(style,catalog,side)
        if not options:
            continue  # Four relevant recorded metrics are required; never invent a fourth.
        source = _four_metric_cells(options[0][0],catalog)
        identity = style['id']
        selected.append({'styleId':identity, 'styleSide':side, 'title':registry[identity][0 if lang=='tr' else 1],
                         'description':DESCRIPTIONS[side][identity][0 if lang=='tr' else 1],
                         'statisticalFit':style['statisticalFit'], 'evidenceCoverage':style['coverage'],
                         'source_metrics':source, 'signals':style['signals'],
                         'secondaryTendency':style.get('secondaryTendency',False),
                         'readingBoundary':registry[identity][2] + (' This is a secondary tendency, not a dominant style. Explicitly describe it as a secondary attacking route, including its measured limitations; do not imply successful execution.' if style.get('secondaryTendency') else '')})
        selected_fits.append(style)
        metric_options.append(options)
        if len(selected) == 2:
            break
    if selected:
        for style,(keys,_) in zip(selected,_distinct_metric_sets(metric_options)):
            style['source_metrics'] = _four_metric_cells(keys,catalog)
    return selected


def bind_selected_styles(draft, selected):
    """Keep classifier identity and authoritative cells fixed through generation/editing."""
    from report_module.profile_evidence import bind_row_evidence
    rows = draft.get('themes') or []
    result = []
    for style in selected:
        row = next((row for row in rows if row.get('styleId') == style['styleId']), None)
        if row is None:
            raise ValueError('Missing selected profile style')
        metadata = {key:style[key] for key in ('styleId','styleSide','title','description','statisticalFit','evidenceCoverage')}
        source = style['source_metrics']
        bound = bind_row_evidence({**row, **metadata}, source, 4)
        # All displayed cells explain the style selection, including valid recorded zeroes.
        bound['metrics'] = list(deepcopy(source).values())
        bound['evidence_values'] = deepcopy(source)
        result.append(bound)
    return result


def _saved_style_themes(selected,profile,lang,side):
    import json
    import re
    from report_module.profile_generation import generate_profile_json
    from report_module.terminology import FOOTBALL_TERMINOLOGY_GUIDANCE
    reused, pending = {}, []
    existing = {theme.get('styleId'):theme for theme in profile.get('themes') or []}
    for style in selected:
        row = existing.get(style['styleId']) or {}
        old = {m.get('label'):m.get('value') for m in row.get('metrics') or []}
        current = {key:value['value'] for key,value in style['source_metrics'].items()}
        if old==current and isinstance(row.get('analysis'),list) and len(row['analysis'])==2:
            bound = bind_selected_styles({'themes':[row]},[style])[0]
            if not bound.get('evidence_validation'):
                reused[style['styleId']] = bound
                continue
        pending.append(style)
    if pending:
        plans = [{key:style[key] for key in ('styleId','title','description','source_metrics','readingBoundary')} for style in pending]
        messages = [('system', "You are ScoutWise Enterprise's team performance analyst. Return JSON with themes: exactly the supplied selected styleIds in order. For each return styleId, analysis as TWO paragraph strings of 40-50 words each, and evidence_metrics as TWO aligned arrays of exact local source_metrics keys. Interpret this team's selected-match attacking/defensive dimension from its four fixed metric cells. Explain a distinct football implication in the second paragraph. Use at most one numerical fact per paragraph. Counts are per-match averages; rates keep their units. Role shares compare players doing the same task. Never invent pitch locations, timing, tactical instructions, causal claims or benchmarks. Obey readingBoundary. No generic recorded-value templates or dictionary definitions. Use natural requested-language football terminology; translate metric references in prose while preserving their exact IDs only in evidence_metrics. Turkish uses dripling and son bölge. No markdown."+FOOTBALL_TERMINOLOGY_GUIDANCE),
                    ('human',json.dumps({'language':lang,'side':side,'selected_styles':plans},ensure_ascii=False))]
        for attempt in range(2):
            draft = generate_profile_json(messages)
            try:
                for row in draft.get('themes') or []:
                    value = row.get('analysis')
                    parts = [value] if isinstance(value,str) else value if isinstance(value,list) else []
                    row['analysis']=[p.strip() for text in parts if isinstance(text,str) for p in re.split(r'\n\s*\n',text) if p.strip()]
                    if len(row['analysis'])!=2:
                        raise ValueError('Each selected style needs exactly two paragraph strings')
                bound = bind_selected_styles(draft,pending)
                issues = [issue for row in bound for issue in row.get('evidence_validation') or []]
                if issues:
                    raise ValueError('; '.join(issues))
                reused.update({row['styleId']:row for row in bound})
                break
            except ValueError as exc:
                if attempt: raise
                messages += [('assistant',json.dumps(draft,ensure_ascii=False)),('human',f'Repair only structure and evidence references: {exc}. Preserve the selected IDs and use only the exact source_metrics keys supplied for each theme.')]
    from report_module.profile_evidence import profile_snapshot
    return profile_snapshot({'themes':[reused[style['styleId']] for style in selected]})['themes']


def _upgrade_style_profile(content, team_id, lang, side):
    field = 'attackProfile' if side == 'attack' else 'defenseProfile'
    profile = content.get(field) or {}
    version = STYLE_VERSION if side == 'attack' else DEFENCE_STYLE_VERSION
    if profile.get('styleVersion') == version or not content.get('reports'):
        return content
    selected = select_team_styles(content['reports'], team_id, lang, side)
    try:
        themes = _saved_style_themes(selected,profile,lang,side)
    except Exception as exc:
        print(f'[enterprise_team_report] event=style_metric_upgrade_deferred side={side} error_type={type(exc).__name__}')
        return content
    result = dict(content)
    result[field] = {**profile, 'themes':themes, 'styleVersion':version}
    if content.get('overview'):
        names = (' ve ' if lang == 'tr' else ' and ').join(theme['title'] for theme in themes)
        summary = f"Seçili maçlarda {names.lower()} özellikleri öne çıkıyor." if lang == 'tr' else f"The selected matches show {names.lower()} characteristics."
        if not themes:
            profile_name = ('hücum' if side == 'attack' else 'savunma') if lang == 'tr' else ('attacking' if side == 'attack' else 'defensive')
            summary = (f"Seçili maçlarda belirgin bir {profile_name} stili için yeterli ayırt edici kanıt bulunmuyor." if lang == 'tr' else f"The selected matches do not establish a sufficiently distinctive {profile_name} style.")
        key = 'attack' if side == 'attack' else 'defense'
        result['overview'] = [{**row, 'summary':summary,
                               'subBullets':[{'label':theme['title'], 'text':theme['analysis'][0]} for theme in themes]}
                              if row.get('key') == key else row for row in content['overview']]
    return result


def upgrade_attack_profile(content, team_id, lang):
    return _upgrade_style_profile(content, team_id, lang, 'attack')


def upgrade_defense_profile(content, team_id, lang):
    return _upgrade_style_profile(content, team_id, lang, 'defence')
