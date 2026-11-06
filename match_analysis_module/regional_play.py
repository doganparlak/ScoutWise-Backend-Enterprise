"""Aggregate match ball locations in the upcoming fixture's home/away frame."""
from __future__ import annotations

import math
from typing import Any

SCOPES = ("overall", "first_half", "second_half")


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def aggregate_regional_play(fixtures: list[dict], team_id: int, target_side: str) -> dict:
    if target_side not in {"home", "away"}:
        raise ValueError("The upcoming fixture side must be home or away")
    counts = {scope: [0] * 9 for scope in SCOPES}
    covered = {scope: set() for scope in SCOPES}
    for fixture_index, fixture in enumerate(fixtures):
        historical_side = next((str((p.get("meta") or {}).get("location") or "").lower()
                                for p in fixture.get("participants") or []
                                if _integer(p.get("id")) == team_id), "")
        if historical_side not in {"home", "away"}:
            continue  # Never infer orientation from participant array order.
        periods = {}
        for period in fixture.get("periods") or []:
            period_type = period.get("type") or {}
            type_id = _integer(period.get("type_id") or period_type.get("id"))
            code = str(period_type.get("code") or period.get("description") or "").lower()
            scope = "first_half" if type_id == 1 or code == "1st-half" else "second_half" if type_id == 2 or code == "2nd-half" else None
            if scope:
                periods[_integer(period.get("id"))] = scope
        fixture_key = fixture.get("id", fixture_index)
        for row in fixture.get("ballcoordinates") or []:
            try:
                x = float(row.get("x")) - .01
                y = (float(row.get("y")) + .02) / 1.04
            except (TypeError, ValueError):
                continue
            if not math.isfinite(x) or not math.isfinite(y):
                continue
            if historical_side != target_side:
                # A 180-degree rotation preserves the team's own left/right flanks.
                x, y = 1 - x, 1 - y
            x, y = min(.999999, max(0, x)), min(.999999, max(0, y))
            zone = int(y * 3) * 3 + int(x * 3)
            scopes = ["overall"]
            period_scope = periods.get(_integer(row.get("period_id")))
            if period_scope:
                scopes.append(period_scope)
            for scope in scopes:
                counts[scope][zone] += 1
                covered[scope].add(fixture_key)
    output = {}
    for scope in SCOPES:
        total = sum(counts[scope])
        output[scope] = {
            "zones": [{"index": i, "count": count, "percentage": round(count / total * 100, 4) if total else 0}
                      for i, count in enumerate(counts[scope])],
            "coordinate_count": total,
            "matches_covered": len(covered[scope]),
        }
    return {
        "territory": output["overall"]["zones"],
        "territory_by_period": output,
        "territory_orientation": "home_left_to_right" if target_side == "home" else "away_right_to_left",
    }


def _compact_pattern(values: list[float], labels: list[str], balanced: str, tr: bool, channels: bool) -> str:
    order = sorted(range(3), key=lambda i: (-values[i], i))
    if values[order[0]] - values[order[-1]] < 5:
        return balanced
    leaders = [i for i in order if values[order[0]] - values[i] < 5]
    if len(leaders) == 2:
        if not channels:
            pair = tuple(sorted(leaders))
            return ({(0, 1): "savunma ve orta bölgeler yakın", (0, 2): "iki kale bölgesi yakın yoğunlukta", (1, 2): "orta ve hücum bölgeleri yakın"} if tr else {(0, 1): "defensive and middle thirds are close", (0, 2): "both goal thirds are similarly concentrated", (1, 2): "middle and attacking thirds are close"})[pair]
        names = " ve ".join(labels[i] for i in sorted(leaders)) if tr else " and ".join(labels[i] for i in sorted(leaders))
        return f"{names} benzer yoğunlukta" if tr else f"{names} similarly concentrated"
    name = labels[order[0]]
    return f"{name} ağırlıklı" if tr and channels else f"{name} yoğun" if tr else f"{name} leads"


def _directional_perspective(usages: list[dict], teams: list[dict], lang: str, scope: str) -> str | None:
    tr = lang == "tr"
    if len(teams) != 2:
        return None
    clauses, peaks = [], []
    for team in teams:
        usage = next((u for u in usages if _integer(u.get("team_id")) == _integer(team.get("id"))), {})
        data = (usage.get("territory_by_period") or {}).get(scope) or {}
        if not data.get("coordinate_count"):
            return None
        values = [float(next((z.get("percentage") for z in data["zones"] if z.get("index") == i), 0) or 0) for i in range(9)]
        away = usage.get("territory_orientation") == "away_right_to_left"
        own = list(reversed(values)) if away else values
        ranked = sorted(range(9), key=lambda i: (-own[i], i))
        first, second = ranked[:2]
        strong = own[first] - own[second] >= 2
        peaks.append((8 - first if away else first, strong))
        name = str(team.get("name") or usage.get("team_id"))
        if max(own) - min(own) < 3:
            clauses.append(f"{name} maçlarında top konumları sahaya dengeli yayıldı" if tr else f"{name} matches showed an even spread of ball locations")
            continue
        row, column = divmod(first, 3)
        regions = (["sol savunma koridorunda", "sol orta koridorda", "sol hücum koridorunda", "savunma merkezinde", "orta alan merkezinde", "hücum merkezinde", "sağ savunma koridorunda", "sağ orta koridorda", "sağ hücum koridorunda"] if tr else ["in the left defensive channel", "in the left midfield channel", "in the left attacking channel", "in central defence", "in central midfield", "in central attack", "in the right defensive channel", "in the right midfield channel", "in the right attacking channel"])
        region = regions[first]
        if not strong:
            second_row, second_column = divmod(second, 3)
            if row == second_row:
                region = (["sol koridor boyunca", "merkez koridor boyunca", "sağ koridor boyunca"] if tr else ["along the left channel", "along the central channel", "along the right channel"])[row]
            elif column == second_column:
                region = (["savunma üçte birinde", "orta üçte birde", "hücum üçte birinde"] if tr else ["in the defensive third", "in the middle third", "in the attacking third"])[column]
            else:
                simple = ["sol savunma", "sol orta", "sol hücum", "savunma merkezi", "orta alan merkezi", "hücum merkezi", "sağ savunma", "sağ orta", "sağ hücum"] if tr else ["left defence", "left midfield", "left attack", "central defence", "central midfield", "central attack", "right defence", "right midfield", "right attack"]
                region = f"{simple[first]} ve {simple[second]} bölgelerinde" if tr else f"around {simple[first]} and {simple[second]}"
        clauses.append(f"{name} maçlarında top konumları {region} yoğunlaştı" if tr else f"{name} matches concentrated ball locations {region}")
    period = ({"overall": "Maç genelinde", "first_half": "İlk yarılarda", "second_half": "İkinci yarılarda"} if tr else {"overall": "Across full matches", "first_half": "In first halves", "second_half": "In second halves"})[scope]
    text = (f"{period}, geçmiş maç kayıtlarına göre " if tr else f"{period}, historical match records show: ") + "; ".join(clauses) + "."
    profiles = []
    for team in teams:
        usage = next(u for u in usages if _integer(u.get("team_id")) == _integer(team.get("id")))
        data = usage["territory_by_period"][scope]
        values = [float(next((z.get("percentage") for z in data["zones"] if z.get("index") == i), 0) or 0) for i in range(9)]
        own = list(reversed(values)) if usage.get("territory_orientation") == "away_right_to_left" else values
        profiles.append({"values": values, "centre": sum(own[3:6]), "attack": sum(own[2::3])})
    first, second = profiles
    overlap = sum(min(a, b) for a, b in zip(first["values"], second["values"]))
    centre_gap = abs(first["centre"] - second["centre"])
    attack_gap = abs(first["attack"] - second["attack"])
    same_peak = all(strong for _, strong in peaks) and peaks[0][0] == peaks[1][0]
    evidence = [
        f"Merkez koridor payları sırasıyla %{first['centre']:.1f} ve %{second['centre']:.1f}; fark {centre_gap:.1f} yüzde puan.",
        f"Takımların kendi hücum üçte birlerindeki konum payları sırasıyla %{first['attack']:.1f} ve %{second['attack']:.1f} olarak ölçüldü.",
        f"İki dağılım aynı saha bölgesinde örtüşüyor; dokuz bölge üzerinden toplam dağılım örtüşmesi %{overlap:.1f}." if same_peak else f"Ortak saha çerçevesinde, dokuz bölge üzerinden hesaplanan dağılım örtüşmesi %{overlap:.1f}; yüksek oran daha benzer konum dağılımını gösterir.",
        f"Hücum üçte biri payları arasındaki fark {attack_gap:.1f} yüzde puan.",
    ] if tr else [
        f"Central channel shares are {first['centre']:.1f}% and {second['centre']:.1f}%, respectively, a gap of {centre_gap:.1f} percentage points.",
        f"Their own attacking thirds contain {first['attack']:.1f}% and {second['attack']:.1f}% of recorded locations, respectively.",
        f"Both distributions overlap in the same pitch region; total overlap across nine zones is {overlap:.1f}%." if same_peak else f"In the shared pitch frame, distribution overlap across nine zones is {overlap:.1f}%; higher overlap means more similar location patterns.",
        f"Attacking third shares differ by {attack_gap:.1f} percentage points.",
    ]
    if same_peak:
        evidence.insert(0, evidence.pop(2))
    for team in reversed(teams):
        usage = next(u for u in usages if _integer(u.get("team_id")) == _integer(team.get("id")))
        periods = usage.get("territory_by_period") or {}
        halves = [periods.get(key) or {} for key in ("first_half", "second_half")]
        if not all(half.get("coordinate_count") for half in halves):
            continue
        thirds = []
        for half in halves:
            values = [float(next((z.get("percentage") for z in half["zones"] if z.get("index") == i), 0) or 0) for i in range(9)]
            if usage.get("territory_orientation") == "away_right_to_left":
                values.reverse()
            thirds.append([sum(values[column::3]) for column in range(3)])
        index = max(range(3), key=lambda i: abs(thirds[1][i] - thirds[0][i]))
        delta = thirds[1][index] - thirds[0][index]
        name = str(team.get("name") or usage.get("team_id"))
        label = (["savunma üçte birinin", "orta üçte birin", "hücum üçte birinin"] if tr else ["defensive third", "middle third", "attacking third"])[index]
        if abs(delta) < 3:
            shift = f"İki yarı arasında {name} maçlarında üçte bir paylarındaki değişimler 3 yüzde puanın altında kaldı." if tr else f"Between halves, {name} matches showed changes below 3 percentage points across all three thirds."
        else:
            shift = f"İlk yarıdan ikinci yarıya {name} maçlarında {label} payı {abs(delta):.1f} yüzde puan {'arttı' if delta > 0 else 'azaldı'}." if tr else f"From first to second half, {name} matches' {label} share {'rose' if delta > 0 else 'fell'} by {abs(delta):.1f} percentage points."
        evidence.insert(1 if same_peak else 0, shift)
    short_clauses = [clause.replace("maçlarında top konumları", "maçlarında") if tr else clause.replace("matches concentrated ball locations", "matches concentrated locations") for clause in clauses]
    short = f"{period}, " + "; ".join(short_clauses) + "."
    for base in (text, short):
        for mask in range(1, 1 << len(evidence)):
            reading = " ".join([base] + [sentence for i, sentence in enumerate(evidence) if mask & (1 << i)])
            if 40 <= len(reading.split()) <= 50:
                return reading
    return text



def regional_perspective(usages: list[dict], teams: list[dict], lang: str, scope: str = "overall") -> str:
    """A compact two-team reading, 40–50 words when both teams have data, with no possession inference."""
    directional = _directional_perspective(usages, teams, lang, scope)
    if directional:
        return directional
    tr = lang == "tr"
    period = ({"overall": "Maç genelinde", "first_half": "İlk yarılarda", "second_half": "İkinci yarılarda"} if tr else {"overall": "Across full matches", "first_half": "In first halves", "second_half": "In second halves"})[scope]
    descriptions = []
    for team in teams[:2]:
        usage = next((u for u in usages if _integer(u.get("team_id")) == _integer(team.get("id"))), {})
        data = (usage.get("territory_by_period") or {}).get(scope) or {}
        name = str(team.get("name") or usage.get("team_id") or "—")
        if not data.get("coordinate_count"):
            descriptions.append(f"{name} için bu dönemde kaydedilmiş top konumu verisi bulunmuyor." if tr else f"No recorded ball-location data is available for {name} in this period.")
            continue
        values = [float(next((z.get("percentage") for z in data["zones"] if z.get("index") == i), 0) or 0) for i in range(9)]
        channels = [sum(values[row * 3:row * 3 + 3]) for row in range(3)]
        thirds = [sum(values[column::3]) for column in range(3)]
        if usage.get("territory_orientation") == "away_right_to_left":
            channels.reverse()
            thirds.reverse()
        channel_labels = ["sol kanat", "merkez", "sağ kanat"] if tr else ["left flank", "centre", "right flank"]
        third_labels = ["savunma üçte biri", "orta üçte bir", "hücum üçte biri"] if tr else ["defensive third", "middle third", "attacking third"]
        channel = _compact_pattern(channels, channel_labels, "kanatlar ve merkez dengeli" if tr else "flanks and centre are balanced", tr, True)
        third = _compact_pattern(thirds, third_labels, "üçte birler dengeli" if tr else "thirds are balanced", tr, False)
        descriptions.append(f"{name} maçlarındaki top konumları: {channel}; {third}." if tr else f"{name} ball locations: {channel}; {third}.")
    text = f"{period}, " + " ".join(descriptions)
    if len(text.split()) > 50:
        text = text.replace("maçlarındaki top konumları", "maçlarında") if tr else text.replace(" ball locations:", ":")
    if len(text.split()) > 50:
        text = text.replace("benzer yoğunlukta", "dengeli") if tr else text.replace(" similarly concentrated", " lead")
    return text


def match_regional_perspective(report: dict, lang: str) -> str:
    """A 40–50 word overall reading for persisted post-match reports."""
    tr = lang == "tr"
    home = next((team for team in report.get("teams", []) if team.get("location") == "home"), {})
    away = next((team for team in report.get("teams", []) if team.get("location") == "away"), {})
    fixture = {"participants": [{"id": 1, "meta": {"location": "home"}}], "ballcoordinates": report.get("ball_coordinates") or []}
    data = aggregate_regional_play([fixture], 1, "home")["territory_by_period"]["overall"]
    if not data["coordinate_count"]:
        return "Seçili dönem için top konumu verisi bulunmuyor." if tr else "No ball-location data is available for the selected period."
    values = [zone["percentage"] for zone in data["zones"]]
    channels = [sum(values[row * 3:row * 3 + 3]) for row in range(3)]
    thirds = [sum(values[column::3]) for column in range(3)]
    home_name = home.get("name") or ("Ev sahibi" if tr else "Home")
    away_name = away.get("name") or ("Deplasman" if tr else "Away")
    channel_names = ["Sol Kanat", "Merkez", "Sağ Kanat"] if tr else ["Left Flank", "Centre", "Right Flank"]
    third_names = [f"{home_name} kalesine yakın üçte bir", "orta üçte bir", f"{away_name} kalesine yakın üçte bir"] if tr else [f"the third nearest {home_name}'s goal", "the middle third", f"the third nearest {away_name}'s goal"]


    channel, third = _concentration(channels, channel_names, tr), _concentration(thirds, third_names, tr)
    base = f"Maç genelinde, {home_name} takımının hücum yönüne göre kanat ve merkez dağılımında {channel}. Sahanın boyuna dağılımında {third}." if tr else f"Across the match, using {home_name}'s attacking direction for flank names, {channel}. Along the pitch, {third}."
    short = f"Maç genelinde, {channel}. {third}." if tr else f"Across the match, {channel}. {third}."
    evidence = [
        f"Merkez koridor, kaydedilen top konumlarının %{channels[1]:.1f} kadarını içeriyor; kalan konumlar iki kanada dağılıyor.",
        f"İki kale üçte birinin payları arasındaki fark {abs(thirds[2] - thirds[0]):.1f} yüzde puan; bu karşılaştırma topun hangi uçta daha sık kaydedildiğini açıklıyor.",
        f"En yoğun ve en az kullanılan koridor arasındaki fark {max(channels) - min(channels):.1f} yüzde puan.",
        f"Orta üçte birin toplam payı %{thirds[1]:.1f} olarak ölçüldü.",
    ] if tr else [
        f"The central channel accounts for {channels[1]:.1f}% of recorded locations, with the remainder distributed across both flanks.",
        f"The two goal thirds differ by {abs(thirds[2] - thirds[0]):.1f} percentage points, quantifying the imbalance between the ends of the pitch.",
        f"The busiest and quietest channels differ by {max(channels) - min(channels):.1f} percentage points.",
        f"The middle third contains {thirds[1]:.1f}% of recorded ball locations.",
    ]
    for opening in (base, short):
        for mask in range(1, 1 << len(evidence)):
            text = " ".join([opening] + [sentence for i, sentence in enumerate(evidence) if mask & (1 << i)])
            if 40 <= len(text.split()) <= 50:
                return text
    return base


def _concentration(shares, names, tr):
    order = sorted(range(3), key=lambda i: -shares[i])
    if shares[order[0]] - shares[order[2]] < 5:
        return "dağılım dengeliydi" if tr else "the distribution was balanced"
    leaders = [i for i in order if shares[order[0]] - shares[i] < 5]
    if len(leaders) == 2:
        a, b = leaders
        return f"{names[a]} (%{shares[a]:.1f}) ve {names[b]} (%{shares[b]:.1f}) yakın yoğunlukla öne çıktı" if tr else f"{names[a]} ({shares[a]:.1f}%) and {names[b]} ({shares[b]:.1f}%) had similarly high concentrations"
    a = leaders[0]
    return f"en yüksek yoğunluk {names[a]} bölgesindeydi (%{shares[a]:.1f})" if tr else f"{names[a]} had the highest concentration ({shares[a]:.1f}%)"


def aggregate_team_regional_play(reports: list[dict], team_id: int) -> dict:
    """Pool records after rotating historical away appearances into the team's frame."""
    fixtures = [{
        "id": (report.get("fixture") or {}).get("id", index),
        "participants": [{"id": team.get("id"), "meta": {"location": team.get("location")}} for team in report.get("teams") or []],
        "periods": report.get("periods") or [],
        "ballcoordinates": report.get("ball_coordinates") or [],
    } for index, report in enumerate(reports)]
    return aggregate_regional_play(fixtures, team_id, "home")


def team_regional_perspective(aggregate: dict, name: str, lang: str, scope: str = "overall", view: str = "team") -> str:
    tr = lang == "tr"
    team_scopes = aggregate["territory_by_period"]
    team_data = team_scopes[scope]
    scopes = team_scopes if view == "team" else {
        period: {**data, "zones": [{**zone, "index": index} for index, zone in enumerate(reversed(data["zones"]))]}
        for period, data in team_scopes.items()
    }
    data = scopes[scope]
    if not data["coordinate_count"]:
        return "Seçili dönem için top konumu verisi bulunmuyor." if tr else "No ball-location data is available for the selected period."
    values = [zone["percentage"] for zone in data["zones"]]
    channels = [sum(values[row * 3:row * 3 + 3]) for row in range(3)]
    thirds = [sum(values[column::3]) for column in range(3)]
    period = ({"overall": "Maç genelinde", "first_half": "İlk yarılarda", "second_half": "İkinci yarılarda"} if tr else {"overall": "Across full matches", "first_half": "In first halves", "second_half": "In second halves"})[scope]
    channel = _concentration(channels, ["Sol", "Merkez", "Sağ"] if tr else ["Left", "Centre", "Right"], tr)
    third = _concentration(thirds, ["Savunma", "Orta Alan", "Hücum"] if tr else ["Defence", "Middle Third", "Attack"], tr)
    team_values = [zone["percentage"] for zone in team_data["zones"]]
    team_thirds = [sum(team_values[column::3]) for column in range(3)]
    dominant_end = 0 if team_thirds[0] >= team_thirds[2] else 2
    own_end = (["savunma", "orta", "hücum"] if tr else ["defensive", "middle", "attacking"])[dominant_end]
    opposite_end = (["hücum", "orta", "savunma"] if tr else ["attacking", "middle", "defensive"])[dominant_end]
    comparison = f"Takımın {own_end} üçte birindeki %{team_thirds[dominant_end]:.1f} konum payı, rakiplerin {opposite_end} bölgesine karşılık geliyor." if tr else f"The team's {own_end} third accounts for {team_thirds[dominant_end]:.1f}% of locations, corresponding to opponents' {opposite_end} third."
    direction = "rakiplerin" if view == "opponents" else "takımın"
    english_direction = "opponents'" if view == "opponents" else "the team's"
    base = f"{period}, {name} maçlarında {direction} hücum yönüne göre {channel}; boyuna dağılımda {third}. {comparison}" if tr else f"{period}, {name} matches, viewed in {english_direction} attacking direction, showed {channel}; {third}. {comparison}"
    short = f"{period}, {name}: {channel}. {third}. {comparison}"
    evidence = [
        f"Merkez koridor payı %{channels[1]:.1f}; savunma ve hücum üçte birlerinin payları arasındaki fark {abs(thirds[2] - thirds[0]):.1f} yüzde puan.",
        f"Hücum üçte biri, kaydedilen konumların %{thirds[2]:.1f} kadarını içerirken orta alanın payı %{thirds[1]:.1f} olarak ölçüldü.",
        f"En yoğun ve en az kullanılan koridor arasındaki fark {max(channels) - min(channels):.1f} yüzde puan.",
    ] if tr else [
        f"The central channel accounts for {channels[1]:.1f}%; defensive and attacking third shares differ by {abs(thirds[2] - thirds[0]):.1f} percentage points.",
        f"The attacking third contains {thirds[2]:.1f}% of recorded locations, compared with {thirds[1]:.1f}% in the middle third.",
        f"The busiest and quietest channels differ by {max(channels) - min(channels):.1f} percentage points.",
    ]
    if scopes["first_half"]["coordinate_count"] and scopes["second_half"]["coordinate_count"]:
        halves = [[sum(zone["percentage"] for zone in scopes[key]["zones"] if zone["index"] % 3 == column) for column in range(3)] for key in ("first_half", "second_half")]
        index = max(range(3), key=lambda i: abs(halves[1][i] - halves[0][i]))
        delta = halves[1][index] - halves[0][index]
        label = (["savunma üçte birinin", "orta üçte birin", "hücum üçte birinin"] if tr else ["defensive third", "middle third", "attacking third"])[index]
        if abs(delta) < 3:
            shift = "İki yarı arasında üçte bir paylarındaki değişimler 3 yüzde puanın altında kaldı." if tr else "Between halves, changes below 3 percentage points across all three thirds."
        else:
            shift = f"İlk yarıdan ikinci yarıya {label} payı {abs(delta):.1f} yüzde puan {'arttı' if delta > 0 else 'azaldı'}." if tr else f"From first to second half, {label} share {'rose' if delta > 0 else 'fell'} by {abs(delta):.1f} percentage points."
        evidence.insert(0, shift)
    for opening in (base, short):
        for mask in range(1, 1 << len(evidence)):
            text = " ".join([opening] + [sentence for i, sentence in enumerate(evidence) if mask & (1 << i)])
            if 40 <= len(text.split()) <= 50:
                return text
    return base
