"""Use the coach classifier over the team's original recorded match snapshots."""
from copy import deepcopy
from math import ceil, isfinite, sqrt
from statistics import mean

STYLE_VERSION = 2
DEFENCE_STYLE_VERSION = 1


def numeric(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def select_team_styles(reports, team_id, lang='en', side='attack'):
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
    selected, selected_fits = [], []
    for style in rank_styles(catalog, side):
        if style['coverage'] <= 0 or style['statisticalFit'] <= 0:
            continue
        if side == 'attack' and not distinct_attack_candidate(style, selected_fits):
            continue
        # The highlighted cards are actual classifier inputs, never unrelated filler.
        core_metrics = {signal['metric'] for signal in style.get('distinctiveSignals') or []}
        signals = sorted(style['signals'], key=lambda signal:(signal['metric'] not in core_metrics,-(signal['weight'] * signal['support'] * signal['coverage'])))
        diagnostic, used = [], set()
        for signal in signals:
            key = signal['metric']
            if key not in used and key in catalog:
                diagnostic.append((key, catalog[key]))
                used.add(key)
            if len(diagnostic) == 4:
                break
        if len(diagnostic) < 3:
            continue
        source = {}
        for key, metric in diagnostic:
            value = metric['seasons'][0]['value']
            shown = f'{value:.2f}'.rstrip('0').rstrip('.')
            basis = 'rate' if metric['percentage'] else 'per100OpponentPasses' if 'Per 100 Opponent Passes' in key else 'per100Passes' if key.endswith(' Per 100 Passes') else 'perMatch'
            source[metric['name']] = {'label':metric['name'], 'value':f'%{shown}' if metric['percentage'] else shown,
                                      'contributes':True, 'sourceKey':key, 'basis':basis,
                                      'matchesCovered':metric['seasons'][0]['covered']}
        identity = style['id']
        selected.append({'styleId':identity, 'styleSide':side, 'title':registry[identity][0 if lang=='tr' else 1],
                         'description':DESCRIPTIONS[side][identity][0 if lang=='tr' else 1],
                         'statisticalFit':style['statisticalFit'], 'evidenceCoverage':style['coverage'],
                         'source_metrics':source, 'signals':style['signals'], 'readingBoundary':registry[identity][2]})
        selected_fits.append(style)
        if len(selected) == 2:
            break
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


def _saved_analysis(style, lang):
    """Source-grounded prose for old snapshots; newly generated reports use the model."""
    names = {
        'Ball Possession %':'Topa sahip olma', 'Passes':'Pas', 'Accurate Passes (%)':'Pas başarı oranı',
        'Long Ball Share (%)':'Uzun pas payı', 'Key Passes':'Kilit pas', 'Total Crosses':'Orta',
        'Cross Share (%)':'Paslarda orta payı', 'Successful Dribbles':'Başarılı dripling',
        'Dribble Accuracy (%)':'Dripling başarı oranı', 'Creator Concentration (%)':'Yaratıcı katkı yoğunluğu',
        'Key Passes Per 100 Passes':'100 pasta kilit pas', 'Through Balls Per 100 Passes':'100 pasta ara pas',
        'Shots Per 100 Passes':'100 pasta şut', 'Shots Total':'Şut', 'Counter Attacks':'Kontratak',
        'Through Balls':'Ara pas', 'Final-third Pass Share (%)':'Son bölge pas payı',
        'Inside-box Shot Share (%)':'Ceza sahası içi şut payı', 'Long Balls Won (%)':'Uzun pas başarı oranı',
        'Tackles':'Müdahale', 'Interceptions':'Pas arası', 'Ball Recovery':'Top kazanımı',
        'Clearances':'Uzaklaştırma', 'Blocked Shots':'Engellenen şut', 'Offsides':'Ofsayt',
        'Total Duels':'İkili mücadele', 'Duels Won':'Kazanılan ikili mücadele', 'Duels Won (%)':'İkili mücadele başarı oranı',
        'Fouls':'Faul', 'Recovery To Loss Ratio':'Top kazanımı / top kaybı',
        'Defensive Role Balance (%)':'Savunma katkısının hatlara dağılımı',
        'Recoveries Per 100 Opponent Passes':'Rakibin 100 pasında top kazanımı',
        'Interceptions Per 100 Opponent Passes':'Rakibin 100 pasında pas arası',
        'Tackles Per 100 Opponent Passes':'Rakibin 100 pasında müdahale',
        'Duels Per 100 Opponent Passes':'Rakibin 100 pasında ikili mücadele',
        'Fouls Per 100 Opponent Passes':'Rakibin 100 pasında faul',
        'Dangerous Attacks':'Tehlikeli hücum',
    }
    def label(cell):
        key = cell['label']
        if lang != 'tr': return key
        if key in names: return names[key]
        if key.startswith('Opponent '): return 'Rakip ' + names.get(key[9:],key[9:]).lower()
        if key.startswith('Outfield '): return 'Saha oyuncuları · ' + names.get(key[9:],key[9:]).lower()
        for role, translated in [('Defenders','Savunma hattı'),('Midfielders','Orta saha'),('Attackers','Hücum hattı')]:
            if key.startswith(role+' ') and key.endswith(' Share (%)'):
                metric = key[len(role)+1:-len(' Share (%)')]
                return f"{translated} · {names.get(metric,metric).lower()} payı"
        return key
    side = style.get('styleSide', 'attack')
    profile_name = ('savunma' if side == 'defence' else 'hücum') if lang == 'tr' else ('defensive' if side == 'defence' else 'attacking')
    cells = list(style['source_metrics'].values())
    paragraphs = []
    for offset in (0, 2):
        a = cells[offset]
        b = cells[min(offset+1,len(cells)-1)]
        value = a['value'].replace('.',',') if lang == 'tr' else a['value'].lstrip('%') + ('%' if a['basis']=='rate' else '')
        unit = ('maç başına ' if lang=='tr' else 'per match ') if a['basis']=='perMatch' else ''
        if lang == 'tr':
            paragraphs.append(f"{label(a)} {unit}{value} olarak kaydediliyor. {label(b)} ile birlikte bu ölçüm, seçili maçlarda {style['title'].lower()} yönündeki {profile_name} profilini destekliyor.")
        else:
            paragraphs.append(f"{label(a)} is recorded at {value} {unit}. Together with {label(b).lower()}, this measurement supports the {style['title'].lower()} {profile_name} profile across the selected matches.")
    return paragraphs


def _upgrade_style_profile(content, team_id, lang, side):
    field = 'attackProfile' if side == 'attack' else 'defenseProfile'
    profile = content.get(field) or {}
    version = STYLE_VERSION if side == 'attack' else DEFENCE_STYLE_VERSION
    if profile.get('styleVersion') == version or not content.get('reports'):
        return content
    selected = select_team_styles(content['reports'], team_id, lang, side)
    themes = []
    for style in selected:
        source = style['source_metrics']
        metric_names = list(source)
        themes.append({**{key:style[key] for key in ('styleId','styleSide','title','description','statisticalFit','evidenceCoverage')},
                       'metrics':list(deepcopy(source).values()), 'analysis':_saved_analysis(style,lang),
                       'evidence_metrics':[metric_names[:2],metric_names[2:]], 'evidence_values':deepcopy(source)})
    result = dict(content)
    result[field] = {**profile, 'themes':themes, 'styleVersion':version}
    if content.get('overview'):
        names = (' ve ' if lang == 'tr' else ' and ').join(theme['title'] for theme in themes)
        summary = f"Seçili maçlarda {names.lower()} özellikleri öne çıkıyor." if lang == 'tr' else f"The selected matches show {names.lower()} characteristics."
        if not themes:
            summary = ("Seçili maçlarda belirgin bir hücum stili için yeterli ayırt edici kanıt bulunmuyor." if lang == 'tr' else "The selected matches do not establish a sufficiently distinctive attacking style.")
        key = 'attack' if side == 'attack' else 'defense'
        result['overview'] = [{**row, 'summary':summary,
                               'subBullets':[{'label':theme['title'], 'text':theme['analysis'][0]} for theme in themes]}
                              if row.get('key') == key else row for row in content['overview']]
    return result


def upgrade_attack_profile(content, team_id, lang):
    return _upgrade_style_profile(content, team_id, lang, 'attack')


def upgrade_defense_profile(content, team_id, lang):
    return _upgrade_style_profile(content, team_id, lang, 'defence')
