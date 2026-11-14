"""Enterprise pre-match additions over the exact selected paired fixtures."""
from datetime import datetime, timezone

from sqlalchemy import text

from api_module.database import engine
from standings_module.metrics import TEAM_ADVANCED_METRICS, numeric
from standings_module.team_derived import DEFINITIONS, build_team_derived


def extend_comparison(categories, rows, team_id):
    """Share league definitions, retaining the report's penalty-xG semantics."""
    # Consume one-shot fixture streams once; retain only advanced totals.
    names = (TEAM_ADVANCED_METRICS | {'Penalties'}) - set(DEFINITIONS) - {'Expected Goals Penalties (pxG)'}
    advanced = {}
    def observed_rows():
        for row in rows:
            metrics = row.get('team_contributions', {}).get(str(team_id), {}).get('metrics') or {}
            for name in names:
                value = numeric(metrics.get(name))
                if value is not None:
                    total = advanced.setdefault(name, [0., 0])
                    total[0] += value
                    total[1] += 1
            yield row
    derived = build_team_derived(observed_rows())
    additions = {}
    for name, metric in derived['teams'].get(str(team_id), {}).items():
        _, category, preference = DEFINITIONS[name]
        additions[name] = (category, {
            'name': name, 'value': metric['perMatch'], 'total': metric['value'],
            'aggregation': 'derived_rate' if metric['aggregation'] == 'derived_rate' else 'per_match',
            'matches_covered': metric['matchesCovered'], 'preference': preference,
        })
    for name, (total, covered) in advanced.items():
        additions[name] = ('set_pieces' if name == 'Penalties' else 'expected', {
            'name': name, 'value': round(total / covered, 4),
            'total': round(total, 4), 'aggregation': 'per_match',
            'matches_covered': covered, 'preference': 'higher',
        })
    # Reopening an old snapshot must not retain stale ranks for replaced values.
    result = {group: [dict(metric) for metric in metrics if metric['name'] not in additions]
              for group, metrics in categories.items()}
    for group, metric in additions.values():
        result.setdefault(group, []).append(metric)
    return {group: sorted(metrics, key=lambda item: item['name']) for group, metrics in result.items() if metrics}


def enrich_selected_team_metrics(usage, fixture):
    """Upgrade saved reports from cached matches, without provider calls on open.

    Require every saved selected fixture; an incomplete cache must never silently
    change a ten-match comparison into a nine-match comparison.
    """
    windows = {}
    for team in usage.get('teams') or []:
        ids = team.get('selected_fixture_ids') or [r.get('fixture_id') for r in team.get('results') or []]
        ids = list(dict.fromkeys(int(fid) for fid in ids if fid))
        if ids and len(ids) == team.get('sample_size'):
            windows[int(team['team_id'])] = ids
    if not windows:
        return usage
    cutoff = datetime.now(timezone.utc)
    ids = list({fid for window in windows.values() for fid in window})
    with engine.connect() as db:
        rows = db.execute(text('''SELECT fixture_id,team_contributions FROM league_match_contributions
            WHERE fixture_id=ANY(CAST(:ids AS bigint[])) AND included_in_aggregation=TRUE
            AND kickoff_at<:cutoff'''), {'ids': ids, 'cutoff': cutoff}).mappings().all()
    by_id = {int(row['fixture_id']): row for row in rows}
    teams = []
    for team in usage.get('teams') or []:
        selected = windows.get(int(team['team_id']), [])
        complete = selected and all(fid in by_id and str(team['team_id']) in by_id[fid]['team_contributions'] for fid in selected)
        teams.append({**team, 'team_comparison': extend_comparison(
            team.get('team_comparison') or {}, [by_id[fid] for fid in selected], team['team_id'])} if complete else team)
    return {**usage, 'teams': teams}


def ensure_team_comparison_perspectives(usage, teams, lang):
    """Fill new sections in saved reports without an AI request on every open."""
    comparisons = usage.get('teams') or []
    if len(comparisons) != 2:
        return usage
    names = {int(team['id']): team.get('name') or str(team['id']) for team in teams}
    left_name, right_name = [names.get(int(team['team_id']), str(team['team_id'])) for team in comparisons]
    perspectives = dict(usage.get('team_comparison_perspectives') or {})
    labels = {
        'Opponent Dangerous Attacks': ('karşılaşılan tehlikeli hücum', 'dangerous attacks faced'),
        'Opponent Shots On Target': ('karşılaşılan isabetli şut', 'shots on target faced'),
        'Opponent Shots Total': ('karşılaşılan şut', 'shots faced'),
        'Opponent Attacks': ('karşılaşılan hücum', 'attacks faced'),
        'Opponent Passes': ('rakip pası', 'opponent passes'),
        'Opponent Counter Attacks': ('karşılaşılan kontratak', 'counterattacks faced'),
        'Opponent Long Balls': ('rakip uzun topu', 'opponent long balls'),
        'Opponent Saves': ('rakip kurtarışı', 'opponent saves'),
    }
    for group in ('opponent_threat', 'opponent_profile'):
        previous = str(perspectives.get(group) or '').strip()
        legacy = ('ilgili göstergede daha az rakip tehdidiyle' in previous
                  or 'Bu bölüm, takımların karşılaştığı rakiplerin oyun profilini betimler' in previous
                  or 'These figures complement attacking output' in previous
                  or 'This section describes the playing profile of opponents faced' in previous)
        if previous and not legacy:
            continue
        metrics = [{m['name']: m for m in team.get('team_comparison', {}).get(group, [])
                    if numeric(m.get('value')) is not None and abs(numeric(m['value'])) > 1e-9}
                   for team in comparisons]
        common = metrics[0].keys() & metrics[1].keys()
        if not common:
            continue
        selected = next((name for name in labels if name in common), None)
        tr = lang == 'tr'
        if selected:
            a, b = (numeric(values[selected]['value']) for values in metrics)
            value = lambda number: f'{number:.2f}'.rstrip('0').rstrip('.').replace('.', ',' if tr else '.')
            lead = (f"Seçili maçlarda maç başına {labels[selected][0]} değeri {left_name} için {value(a)}, {right_name} için {value(b)}."
                    if tr else f"Across the selected matches, {labels[selected][1]} per match are {value(a)} for {left_name} and {value(b)} for {right_name}.")
        else:
            lead = (f"{left_name} ile {right_name}, seçili maçlarda iki taraf için de mevcut ortak göstergeler üzerinden karşılaştırılıyor."
                    if tr else f"{left_name} and {right_name} are compared using recorded indicators available for both sides across the selected matches.")
        if group == 'opponent_threat':
            if selected and abs(a - b) / max(abs(a), abs(b)) > 0.10:
                less, more = (left_name, right_name) if a < b else (right_name, left_name)
                detail = (f"Bu fark, {less} tarafının rakip üretimini sınırlamada daha olumlu bir geçmişe sahip olduğuna işaret ediyor. Yaklaşan maçta {more}, rakibin hücum sürekliliğini kesmekte zorlanırsa baskı altında daha uzun süre kalabilir. {less} için bu alan, hücumlarını tekrarlayarak rakibinin savunma direncini sınama fırsatı yaratabilir."
                          if tr else f"This gap suggests {less} has a stronger recent record of limiting this aspect of opponent output. In the upcoming match, {more} could spend longer under pressure if opposing attacks keep recurring. Sustaining attacks may therefore give {less} an opportunity to test the opposing defence's resilience.")
            else:
                detail = ("Bu yakınlık, rakibe bırakılan üretim açısından belirgin bir ayrışmaya işaret etmiyor. Yaklaşan karşılaşmada hücumlarını daha sık tekrarlayan taraf, benzer baskıya alışmış rakibinin savunma direncini sınayabilir. Maçın bu alanındaki dengeyi, hücumların devamlılığı ve savunma sonrasında yeniden topa sahip olabilme becerisi şekillendirebilir."
                          if tr else "The similar figures suggest little separation in this aspect of opponent output. In the upcoming match, the side sustaining repeated attacks could test an opponent accustomed to comparable pressure. Attacking continuity and the ability to regain possession after defending may shape the balance in this area.")
                if not selected:
                    detail = ("Ortak göstergeler, yaklaşan maçta rakip üretiminin hangi alanlarda sınırlandırılabileceğini değerlendirmek için kullanılabilir. Hücumlarını sürdürebilen taraf, karşı savunmayı tekrarlanan baskı altında sınama fırsatı bulabilir."
                              if tr else "The shared indicators help identify where opponent output could be restricted in the upcoming match. Sustained attacks may provide opportunities to test the opposing defence under repeated pressure.")
        else:
            if selected:
                similar = abs(a - b) / max(abs(a), abs(b)) <= 0.10
                more = left_name if a > b else right_name
                contrast = ("İki takım bu alanda benzer rakip profilleriyle karşılaşmış."
                            if similar else f"{more}, bu göstergenin daha yüksek olduğu rakip maçları oynamış.") if tr else (
                                "Both teams have faced similar opponent profiles in this area." if similar
                                else f"{more} has faced opponents with higher output in this indicator.")
            else:
                contrast = ("Ortak göstergeler, iki takımın karşılaştığı oyun koşullarını birlikte değerlendirme olanağı veriyor."
                            if tr else "The shared indicators provide context for the playing conditions both teams have faced.")
            implications = {
                'Opponent Passes': (
                    "Yaklaşan maçta rakibin pas bağlantılarını kesebilmek ve kazanılan topları koruyabilmek, oyunun hangi yarı sahada daha uzun oynanacağını etkileyebilir. Pas hacmindeki bu bağlam, yerleşik hücumlarla karşılaşma deneyimi üzerinden maçın olası akışını okumayı sağlıyor.",
                    "Disrupting passing connections and retaining regained possession could influence where the upcoming match is played. This context suggests that experience facing sustained passing sequences may matter when either side attempts to establish longer attacking spells."),
                'Opponent Counter Attacks': (
                    "Yaklaşan maçta top kaybı sonrasındaki yerleşim ve geçiş hücumlarını yavaşlatabilmek belirleyici olabilir. Hücum ederken geride bırakılan alanlar, bu karşılaşmada rakibin hızla ilerleyebileceği fırsatlara dönüşebilir; top kayıplarının yeri bu nedenle maçın akışında önem kazanabilir.",
                    "Positioning after losing possession and slowing transitions could matter in the upcoming match. Space left behind attacks may become opportunities for opponents to advance quickly, making the location of turnovers relevant to the flow of the game."),
                'Opponent Long Balls': (
                    "Yaklaşan maçta ilk hava topunun ardından ikinci topları toplayabilmek, hücumların devamlılığını etkileyebilir. Uzun pasların yöneldiği bölgelerde oluşacak mücadeleler, takımlardan birinin rakip yarı sahada daha uzun kalmasına veya oyunun daha sık yön değiştirmesine zemin hazırlayabilir.",
                    "Collecting second balls after the initial aerial contest could influence attacking continuity in the upcoming match. Contests around long-ball landing zones may allow either side to sustain territory or lead to more frequent changes of possession."),
                'Opponent Saves': (
                    "Yaklaşan maçta üretilen isabetli şutların golle sonuçlanması, rakip kaleciye çıkarılan işin skor karşılığını belirleyecek. Hücumları daha net bitiriş fırsatlarına dönüştürebilen taraf, kaleci müdahaleleriyle sonuçlanan üretimini skora taşıma şansını artırabilir.",
                    "Converting shots on target will determine whether testing the opposing goalkeeper translates into goals in the upcoming match. Creating clearer finishing opportunities could help either side turn goalkeeper interventions into a greater chance of scoring."),
            }
            default = ("Yaklaşan maçta bu rakip alışkanlıklarına verilecek karşılık, topun kazanıldığı bölgeleri ve hücumların devamlılığını etkileyebilir. İki takımın farklı oyun koşullarına nasıl uyum sağlayacağı, karşılaşmanın olası akışını değerlendirirken takip edilecek noktalardan biri olacak.",
                       "Responses to these opponent tendencies could affect where possession is regained and how attacks are sustained in the upcoming match. Adaptation to these playing conditions may help shape the flow of the encounter.")
            detail = f"{contrast} {implications.get(selected, default)[0 if tr else 1]}"
        perspectives[group] = f'{lead} {detail}'
    return {**usage, 'team_comparison_perspectives': perspectives}
