"""Fixed coach archetypes, with explicit limits on what match statistics establish."""
import json
import os
from copy import deepcopy
from itertools import combinations
from .classification import rank_styles

ATTACK={
 'positional':('Pozisyonel Hücumcu','Positional Attacker','Possession, pass accuracy, formation continuity and role participation are proxies; never assert spacing or off-ball positioning.'),
 'possession':('Dominant Topa Sahip Olan','Dominant Possession','Use sustained possession, passing volume and accuracy with the outfield groups carrying circulation.'),
 'direct':('Direkt Hücumcu','Direct Attacker','Use recorded long-ball share and carrying/shooting contributions; do not invent attack speed or passing-sequence length.'),
 'transition':('Geçiş Hücumcusu','Transition Attacker','Use recorded counterattacks when present, shots relative to passes, carrying, recovery workload and possession as a transition-style fit; do not invent timed sequences.'),
 'combination':('Kombinasyon Hücumcusu','Combination Attacker','Passing accuracy, low long-ball share and creativity shares are proxies; no claim of observed overloads or intricate sequences.'),
 'wide':('Kanat Odaklı Hücumcu','Wing-oriented Attacker','Use crossing and dribbling output plus contributing role groups; do not infer individual winger/fullback positions.'),
 'vertical':('Dikey Hücumcu','Vertical Attacker','Use final-third passing, through balls, inside-box shot share and shots per pass as progression proxies; do not invent tracked central paths.'),
 'pragmatic':('Pragmatik Hücumcu','Pragmatic Attacker','Describe measured changes in approach between periods or matches, not inferred coaching intent or opponent-specific instructions.'),
 'free':('Serbest Hücumcu','Free Attacker','Use dribbling success, creator concentration and attacker carrying contributions as an individual-initiative fit; do not invent off-ball freedom.'),
}
DEFENCE={
 'aggressive':('Agresif Presçi','Aggressive Presser','Use opponent-pass-adjusted tackles and duels, forward tackling and fouls as an active-pressure fit, not measured pressing events.'),
 'high':('Önde Baskıcı','High-line Presser','Use opponent offsides, opponent progression and long-ball share, possession and forward recovery shares as a high-defending fit; do not assert an observed line height.'),
 'midblock':('Orta Blok Savunmacısı','Mid-block Defender','Use midfield interception/recovery shares, adjusted interceptions and opponent creation as a midfield-defending fit; do not invent compactness measurements.'),
 'deep':('Derin Blok Savunmacısı','Deep-block Defender','Use clearances, defensive blocks, defender clearance shares, possession and opponent box-shot share as a goal-protection fit; do not assert a tracked block depth.'),
 'man':('Adam Odaklı Savunmacı','Man-oriented Defender','Use duel workload and success, defender duel contribution, tackles and fouls as a confrontation-oriented fit; do not invent marking assignments.'),
 'zone':('Alan Odaklı Savunmacı','Zonal Defender','Use interception activity, the distribution of recovery work, foul restraint and opponent creation/box-shot share as a space-protection fit; do not invent positional coverage.'),
 'counterpress':('Geçiş Presçisi','Counterpresser','Use recoveries relative to opponent passes, forward/midfield recovery shares, recorded losses and opponent counterattacks as a recovery-pressure fit; do not invent post-loss timing.'),
 'reactive':('Reaktif Savunmacı','Reactive Defender','Statistical fit only: opponent output, possession and role recovery distribution; do not invent block depth or a deliberate reactive instruction.'),
 'pragmatic':('Pragmatik Savunmacı','Pragmatic Defender','Statistical fit only: measured variations in workload and recovery roles, not inferred opponent-specific plans.'),
}


DESCRIPTIONS={
 'attack':{
 'positional':('Yapılandırılmış pozisyon oyunu, alan paylaşımı ve kontrollü topa sahip olma.','Structured positional play, spacing and controlled possession'),
 'possession':('Sürdürülen topa sahip olma üstünlüğüyle maçları kontrol eder.','Controls matches through sustained possession'),
 'direct':('Daha az pas bağlantısıyla hızlı ve dikine ilerlemeyi amaçlar.','Rapid vertical progression, minimal passing sequences'),
 'transition':('Kontrataklara ve açık alanlara hücum etmeye öncelik verir.','Prioritizes counterattacks and attacking open spaces'),
 'combination':('Kısa kombinasyonlar, sayısal üstünlükler ve bağlantılı pas oyunu.','Short combinations, overloads and intricate passing'),
 'wide':('Genişliği, kanat oyuncularını ve orta fırsatlarını kullanır.','Uses width, wide players and crossing opportunities'),
 'vertical':('Merkezdeki alanları kullanarak kaleye doğru kararlı biçimde ilerler.','Aggressively progresses toward goal through central spaces'),
 'pragmatic':('Hücum stratejisini oyuncu özelliklerine ve rakibe göre uyarlar.','Adapts attacking strategies to available players and opponents'),
 'free':('Yaratıcılığı, pozisyon serbestliğini ve doğaçlamayı teşvik eder.','Encourages creativity, positional freedom and improvisation')},
 'defence':{
 'aggressive':('Yüksek şiddetli pres ve proaktif top kazanımı.','High-intensity pressing, proactive ball recovery'),
 'high':('İleri savunma çizgisiyle sahayı önde savunur.','Defends high up the pitch with an advanced defensive line'),
 'midblock':('Orta saha bölgelerinde kompakt bir savunma yapısı kurar.','Compact defensive structure in midfield zones'),
 'deep':('Kaleye yakın savunur, alan korumaya öncelik verir.','Defends close to goal, prioritizing space protection'),
 'man':('Adam odaklı eşleşmeler ve pres görevleri kullanır.','Uses man-oriented marking and pressing assignments'),
 'zone':('Pozisyon disiplinine ve alan kontrolüne öncelik verir.','Prioritizes positional discipline and space control'),
 'counterpress':('Top kaybının hemen ardından baskı uygular.','Immediately applies pressure after losing possession'),
 'reactive':('Baskıyı başlatmaktan çok rakibin aksiyonlarına karşılık verir.','Responds to opponent actions rather than initiating pressure'),
 'pragmatic':('Savunma stratejisini rakibin güçlü yönlerine göre uyarlar.','Adjusts defensive strategies based on opponent strengths')}}


# Each class has its own diagnostic team metrics and relevant outfield contributions.
BUNDLES = {
 'attack': {
  'possession': (['Ball Possession %','Passes','role:midfielder:Passes','Accurate Passes (%)'], ['Passes','Key Passes']),
  'positional': (['Final-third Pass Share (%)','Outfield Final-third Pass Share (%)','Accurate Passes (%)','role:midfielder:Passes','role:defender:Passes','Key Passes'], ['Passes','Passes In Final Third']),
  'direct': (['Long Ball Share (%)','Shots Per 100 Passes','role:attacker:Shots Total','Long Balls Won (%)'], ['Shots Total','Successful Dribbles']),
  'transition': (['Counter Attacks','Shots Per 100 Passes','role:attacker:Successful Dribbles','Recoveries Per 100 Opponent Passes','Outfield Recoveries Per 100 Opponent Passes','Shots Total'], ['Successful Dribbles','Shots Total']),
  'combination': (['role:midfielder:Key Passes','Accurate Passes (%)','Long Ball Share (%)','Key Passes'], ['Key Passes','Passes']),
  'wide': (['Cross Share (%)','Total Crosses','role:midfielder:Total Crosses','Accurate Crosses (%)','Successful Dribbles'], ['Total Crosses','Successful Dribbles']),
  'vertical': (['Through Balls','Outfield Through Balls','Final-third Pass Share (%)','Outfield Final-third Pass Share (%)','role:midfielder:Through Balls','Inside-box Shot Share (%)','Passes In Final Third'], ['Through Balls','Passes In Final Third','Key Passes']),
  'pragmatic': (['Long Ball Share (%)','Cross Share (%)','role:midfielder:Key Passes','Shots On Target (%)','Ball Possession %'], ['Key Passes','Shots Total']),
  'free': (['Creator Concentration (%)','Successful Dribbles','role:attacker:Successful Dribbles','Dribble Accuracy (%)','Key Passes'], ['Successful Dribbles','Key Passes']),
 },
 'defence': {
  'aggressive': (['Tackles Per 100 Opponent Passes','Outfield Tackles Per 100 Opponent Passes','role:attacker:Tackles','Duels Per 100 Opponent Passes','Fouls Per 100 Opponent Passes','Tackles','Total Duels'], ['Tackles','Total Duels','Fouls']),
  'high': (['Opponent Offsides','role:attacker:Ball Recovery','Opponent Final-third Pass Share (%)','Opponent Long Ball Share (%)','Opponent Shots Total'], ['Ball Recovery','Interceptions']),
  'midblock': (['role:midfielder:Interceptions','role:midfielder:Ball Recovery','Interceptions Per 100 Opponent Passes','Opponent Key Passes','Interceptions'], ['Interceptions','Ball Recovery']),
  'deep': (['Clearances','Blocked Shots','role:defender:Clearances','Opponent Inside-box Shot Share (%)','Outfield Clearances'], ['Clearances','Aerials Won','Ball Recovery']),
  'man': (['Duels Per 100 Opponent Passes','Outfield Duels Per 100 Opponent Passes','Duels Won (%)','Outfield Duels Won (%)','role:defender:Duels Won','Fouls','Total Duels'], ['Duels Won','Tackles','Total Duels']),
  'zone': (['Defensive Role Balance (%)','Interceptions Per 100 Opponent Passes','Outfield Interceptions Per 100 Opponent Passes','Opponent Inside-box Shot Share (%)','Opponent Key Passes','Interceptions'], ['Interceptions','Ball Recovery']),
  'counterpress': (['Recoveries Per 100 Opponent Passes','Outfield Recoveries Per 100 Opponent Passes','role:attacker:Ball Recovery','role:midfielder:Ball Recovery','Recovery To Loss Ratio','Outfield Recovery To Loss Ratio'], ['Ball Recovery','Interceptions']),
  'reactive': (['Opponent Shots Total','role:defender:Ball Recovery','Opponent Dangerous Attacks','Ball Possession %','Blocked Shots'], ['Ball Recovery','Clearances','Interceptions']),
  'pragmatic': (['Opponent Shots Total','role:midfielder:Tackles','role:defender:Interceptions','Opponent Key Passes'], ['Tackles','Interceptions','Ball Recovery']),
 },
}


def fit_candidates(catalog, side):
    registry = ATTACK if side == 'attack' else DEFENCE
    return [{**row, 'label':{'tr':registry[row['id']][0], 'en':registry[row['id']][1]},
        'readingBoundary':registry[row['id']][2], 'evidenceType':'statistical_fit'} for row in rank_styles(catalog, side)]


def default_plan(archetype, catalog, side):
    preferred, players = BUNDLES[side][archetype]
    fallback = (['Key Passes','Shots Total','Passes','Accurate Passes (%)','Successful Dribbles','role:midfielder:Passes'] if side == 'attack'
        else ['Opponent Shots Total','Interceptions','Tackles','Ball Recovery','Clearances','Outfield Interceptions','Outfield Tackles','role:midfielder:Tackles'])
    keys = list(dict.fromkeys(key for key in preferred + fallback if key in catalog))[:8]
    return {'id':archetype, 'metrics':keys[:4], 'metricPool':keys,
        'primaryMetric':keys[0] if keys else '', 'playerMetrics':players}


def materialize_supported(plan, seasons, meta, side):
    from . import profiles as engine
    keys = plan.get('metricPool', plan['metrics'])
    recorded = {key for season in seasons for player in season['players'] for key in player['totals']}
    supported_players = [key for key in plan['playerMetrics'] if key in recorded]
    if supported_players:
        plan = {**plan, 'playerMetrics':supported_players}
    # A missing fourth metric must not discard a coherent three-metric explanation.
    for size in (4, 3):
        for subset in combinations(keys, size):
            candidate = {**plan, 'metrics':list(subset), 'primaryMetric':subset[0]}
            topic = engine.materialize(candidate, seasons, meta, side)
            if topic is not None:
                return topic
    return None


def normalize_plan(plan,meta):
    keys=plan.get('metrics',[])
    if not isinstance(keys,list):return plan
    keys=list(dict.fromkeys(k for k in keys if isinstance(k,str) and k in meta))
    primary=plan.get('primaryMetric')
    if primary not in meta:primary=keys[0] if keys else ''
    # Some responses list table metrics separately. Merge, then retain 3–4 total.
    ordered=([primary] if primary else [])+[k for k in keys if k!=primary]
    if len(ordered)>4:
        roles=[k for k in ordered if k.startswith('role:')]
        paired=next(([k for k in roles if meta[k].get('playerMetric')==meta[r].get('playerMetric')] for r in roles
                     if sum(meta[k].get('playerMetric')==meta[r].get('playerMetric') for k in roles)>1),[])
        selected=[primary]+[k for k in paired if k!=primary][:2]
        opponent=next((k for k in ordered if k.startswith('Opponent ') and k not in selected),None)
        if opponent:selected.append(opponent)
        selected += [k for k in ordered if k not in selected][:4-len(selected)]
        ordered=selected[:4]
    return {**plan,'metrics':ordered,'primaryMetric':primary}


def generate(seasons, meta, catalog, perspective, allow_ai=True, fixed_topics=None):
    from . import profiles as engine
    options = {side:fit_candidates(catalog, side) for side in ('attack','defence')}
    output = deepcopy(fixed_topics) if fixed_topics is not None else {}
    if fixed_topics is None:
        for side in ('attack','defence'):
            topics = []
            for option in options[side]:
                plan = default_plan(option['id'], catalog, side)
                topic = materialize_supported(plan, seasons, meta, side)
                if topic is None:
                    continue
                topic.update({'id':option['id'], 'title':option['label'], 'evidenceType':'statistical_fit',
                    'statisticalFit':option['statisticalFit'], 'evidenceCoverage':option['coverage'],
                    'description':{'tr':DESCRIPTIONS[side][option['id']][0], 'en':DESCRIPTIONS[side][option['id']][1]}})
                topic['summary'] = {lang:engine.fallback_explanation(topic, meta, lang) for lang in ('tr','en')}
                topics.append(topic)
                if len(topics) == 2:
                    break
            output[side] = topics
    if not allow_ai or not os.getenv('OPENAI_API_KEY'):
        return output
    selected = {side:[{key:topic[key] for key in ('id','rows','primaryMetric','playerMetricKeys')} for topic in topics] for side,topics in output.items()}
    payload = {'selectedTopics':selected, 'archetypes':{side:[option for option in options[side] if option['id'] in {t['id'] for t in output[side]}] for side in output}, 'seasons':[]}
    for season in seasons:
        payload['seasons'].append({'id':season['period']['id'],'team':season['period']['team'],'season':season['period']['season'],
            'players':[{k:p[k] for k in ('id','role','minutes','totals')} for p in sorted(season['players'],key=lambda p:-p['minutes'])[:14]]})
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(model=os.getenv('OPENAI_MATCH_REPORT_MODEL',os.getenv('OPENAI_REPORT_MODEL','gpt-5.6-luna')),
            api_key=os.environ['OPENAI_API_KEY'], temperature=.2, timeout=50, max_retries=0).invoke([
            ('system', 'Return JSON {attack:[{id,summary:{tr,en}}],defence:[{id,summary:{tr,en}}]}. Each side has exactly the supplied two selected IDs, or fewer only when fewer are supplied. Never change IDs, invent classes, or return titles or metrics. All eighteen coach archetypes are evaluated using recorded team and outfield-player statistics; their scores are statistical style matches. Read each supplied readingBoundary and explain the measured relationships supporting the selected style. Each summary is EXACTLY TWO natural football sentences, 25-45 words per language. Lead with the actual football takeaway, not definitions or report instructions. Identify the role groups contributing to the relevant task using role shares and player totals, comparing groups doing the SAME task. Explain differences between periods only when recorded. Do not contrast defenders passing with attackers shooting. No numbers, dates, player names, goalkeeping, recommendations or generic caveats. Never say sınırlı, atıf, istatistiksel bileşim, sınıflandırmasını destekliyor, birlikte okunuyor or kanıtlamıyor. Use kilit pas, tehlikeli hücum and başarı oranı in Turkish. Aggregate metrics support a style interpretation, not directly observed marking assignments, exact block height, attack speed, off-ball freedom or timed post-loss pressing; do not invent those observations or claim coaching intent. Observe these boundaries silently; do not print caveats about unmeasured information. Use saha oyuncuları and son bölge in Turkish. Recovery To Loss Ratio compares aggregated totals, not a timed or sequential recovery after a specific loss. Do not infer half-space play from inside-box shots, or a fullback/centre-back role from the generic defender group. Do not claim chronological development between different clubs. For one season describe its matches, not differences between seasons.'),
            ('human',json.dumps(payload))])
        raw = str(response.content).strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        ai = json.loads(raw)
    except Exception:
        return output
    for side, topics in output.items():
        entries = ai.get(side, []) if isinstance(ai, dict) else []
        summaries = {entry['id']:entry.get('summary', {}) for entry in entries if isinstance(entry, dict) and isinstance(entry.get('id'), str)} if isinstance(entries, list) else {}
        for topic in topics:
            proposed = summaries.get(topic['id'])
            for lang in ('tr','en'):
                text = proposed.get(lang, '') if isinstance(proposed, dict) else ''
                sentences = len(engine.re.split(r'(?<=[.!?])\s+', text.strip())) if isinstance(text, str) else 0
                valid = engine.valid_text(text, 45) and len(text.split()) >= 24 and sentences == 2 and not engine.mentions_player(text, seasons)
                if isinstance(text, str) and engine.re.search(r'istatistiksel bileşim|sınıflandırmasını destekliyor|birlikte okunuyor|kanıtlamıyor|ancak ölçüm|ölçüm.*zamanlama|does not measure|measurement does not|top kaybı sonrası toparlanma', text, engine.re.I):
                    valid = False
                if len(seasons) == 1 and isinstance(text, str) and engine.re.search(r'farklı dönem|sezonlar arasında|different seasons|across periods', text, engine.re.I):
                    valid = False
                if valid:
                    topic['summary'][lang] = text.strip()
    return output
