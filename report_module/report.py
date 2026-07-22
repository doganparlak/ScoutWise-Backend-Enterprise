from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek
from sqlalchemy import text

from constants_module.constants import ROLE_LONG_TO_SHORT, ROLE_SHORT_TO_LONG
from report_module.prompts import report_system_prompt
from report_module.utilities import _first_non_empty, _normalize_roles, _score_candidate, norm_name

load_dotenv()

CHAT_LLM = ChatDeepSeek(model="deepseek-chat", temperature=0.3)

_report_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", report_system_prompt),
        ("human", "lang: {lang}\n\n{input_text}"),
    ]
)

report_chain = _report_prompt | CHAT_LLM | StrOutputParser()

ROLE_USAGE_CONSTRAINTS: Dict[str, Dict[str, Any]] = {
    "GK": {
        "allowed": "goalkeeper only",
        "forbidden": "outfield roles such as defender, midfielder, winger, forward, striker",
    },
    "LB": {
        "allowed": "left back / fullback only",
        "forbidden": "center midfield, number 8, winger, striker, center forward, goalkeeper",
    },
    "RB": {
        "allowed": "right back / fullback only",
        "forbidden": "center midfield, number 8, winger, striker, center forward, goalkeeper",
    },
    "CB": {
        "allowed": "center back only",
        "forbidden": "fullback, center midfield, number 8, winger, striker, center forward, goalkeeper",
    },
    "LM": {
        "allowed": "left midfield / wide midfielder only",
        "forbidden": "center back, fullback, defensive midfielder, striker, goalkeeper",
    },
    "RM": {
        "allowed": "right midfield / wide midfielder only",
        "forbidden": "center back, fullback, defensive midfielder, striker, goalkeeper",
    },
    "CDM": {
        "allowed": "defensive midfielder / holding midfielder only",
        "forbidden": "center forward, striker, winger, fullback, center back, goalkeeper",
    },
    "CM": {
        "allowed": "central midfielder / number 8 only",
        "forbidden": "center forward, striker, winger, fullback, center back, goalkeeper",
    },
    "CAM": {
        "allowed": "attacking midfielder / number 10 only",
        "forbidden": "center forward, striker, fullback, center back, goalkeeper",
    },
    "LW": {
        "allowed": "left winger / left wide forward only",
        "forbidden": "center midfield, number 8, defensive midfielder, fullback, center back, goalkeeper",
    },
    "RW": {
        "allowed": "right winger / right wide forward only",
        "forbidden": "center midfield, number 8, defensive midfielder, fullback, center back, goalkeeper",
    },
    "CF": {
        "allowed": "striker / center forward only",
        "forbidden": "center midfield, number 8, attacking midfielder, defensive midfielder, winger, fullback, center back, goalkeeper",
    },
}

ATTACK_LINE_PHASE_ROLES = {"LM", "RM", "LW", "RW", "CAM", "CF"}
CENTER_LINE_PHASE_ROLES = {"CDM", "CM"}
BACK_LINE_PHASE_ROLES = {"LB", "RB", "CB"}
WIDE_PHASE_ROLES = {"LW", "RW", "LM", "RM"}
FULLBACK_PHASE_ROLES = {"LB", "RB"}
ATTACK_LINE_PHASES = ["Progression", "Final Third", "High Block", "Mid Block"]
OUTFIELD_PHASES = ["Build-up", "Progression", "Final Third", "High Block", "Mid Block", "Low Block"]
GOALKEEPER_PHASES = ["Build-up", "Low Block"]
PHASE_ROLE_TAXONOMY: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "GK": {
        "Build-up": [
            {
                "name": "Safe-Passing Goalkeeper",
                "description": "Savunmadan çıkışta düşük riskli pasları tercih eder; stoperler ve beklerle kısa pas bağlantısı kurarak takımın topa sahip olmasını sürdürür.",
                "metrics": [
                    "Passes",
                    "Accurate Passes",
                    "Accurate Passes (%)",
                    "Touches",
                    "Backward Passes",
                    "Possession Lost",
                    "Turn Over",
                    "Error Lead To Shot",
                    "Error Lead To Goal",
                ],
            },
            {
                "name": "Long-Distribution Goalkeeper",
                "description": "Rakibin baskısını uzun paslarla aşarak topu doğrudan orta saha veya hücum hattına ulaştırmaya çalışır.",
                "metrics": [
                    "Long Balls",
                    "Long Balls Won",
                    "Long Balls Won (%)",
                    "Passes",
                    "Accurate Passes",
                    "Accurate Passes (%)",
                    "Possession Lost",
                    "Turn Over",
                ],
            },
            {
                "name": "Playmaking Goalkeeper",
                "description": "Kısa ve uzun pasları birlikte kullanır; rakibin baskısına göre çıkış yönünü belirler ve takımın oyun kurulumuna aktif olarak katılır.",
                "metrics": [
                    "Touches",
                    "Passes",
                    "Accurate Passes",
                    "Accurate Passes (%)",
                    "Long Balls",
                    "Long Balls Won",
                    "Long Balls Won (%)",
                    "Backward Passes",
                    "Possession Lost",
                    "Error Lead To Shot",
                ],
            },
        ],
        "Low Block": [
            {
                "name": "Line Goalkeeper",
                "description": "Kaleye yakın pozisyon alarak ceza sahası içindeki şutlara karşı kaleyi korur; önceliği savunma arkasına çıkmak yerine çizgi üzerindeki tehditleri karşılamaktır.",
                "metrics": [
                    "Goals Conceded",
                    "Blocked Shots",
                    "Rating",
                    "Error Lead To Goal",
                    "Error Lead To Shot",
                    "Own Goals",
                    "Match Count",
                    "Minutes Played",
                ],
            },
            {
                "name": "Box-Commanding Goalkeeper",
                "description": "Ortalar, duran toplar ve hava toplarında ceza sahasına müdahale eder; hava toplarında fiziksel üstünlük kurarak savunmanın üzerindeki baskıyı azaltır.",
                "metrics": [
                    "Aerials",
                    "Aerials Won",
                    "Aerials Won (%)",
                    "Clearances",
                    "Clearance Offline",
                    "Ball Recovery",
                    "Total Duels",
                    "Duels Won",
                    "Duels Won (%)",
                ],
            },
            {
                "name": "Last-Action Goalkeeper",
                "description": "Savunma hattı geçildiğinde son müdahaleyi yapan oyuncudur; kritik pozisyonlarda topu uzaklaştırır ve doğrudan gole veya şuta yol açabilecek hataları sınırlamaya çalışır.",
                "metrics": [
                    "Last Man Tackle",
                    "Clearances",
                    "Clearance Offline",
                    "Ball Recovery",
                    "Goals Conceded",
                    "Error Lead To Goal",
                    "Error Lead To Shot",
                    "Fouls",
                    "Penalties Committed",
                ],
            },
        ],
    },
    "CF": {
        "Build-up": [
            {"name": "Defense-Pinning Forward", "description": "Rakip stoperleri geriye iterek takımın ilk bölgeden çıkmasına alan sağlar.", "metrics": ["Offsides", "Shots Total", "Touches", "Fouls Drawn", "Total Duels", "Aerials"]},
            {"name": "Depth-Running Forward", "description": "İlk bölgeden atılacak dikey paslar için savunma arkasına koşu tehdidi sunar.", "metrics": ["Offsides", "Shots Total", "Shots On Target", "Expected Goals", "Touches", "Fouls Drawn"]},
            {"name": "Long-Ball Target", "description": "Uzun paslarda hedef olur; hava ve fiziksel mücadelelerle ikinci top fırsatı sağlar.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Total Duels", "Duels Won", "Duels Won (%)", "Long Balls Won", "Fouls Drawn", "Possession Lost"]},
        ],
        "Progression": [
            {"name": "Depth-Running Forward", "description": "İkinci bölgede savunma arkasına koşularla dikey pas seçeneği oluşturur.", "metrics": ["Offsides", "Shots Total", "Shots On Target", "Expected Goals", "Touches", "Fouls Drawn"]},
            {"name": "Ball-Securing Forward", "description": "Sırtı dönük oyunda topu koruyarak takım arkadaşlarının hücuma katılmasını sağlar.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Dispossessed", "Possession Lost", "Fouls Drawn", "Accurate Passes (%)"]},
            {"name": "Link Forward", "description": "İkinci bölgede topu kanatlara veya hücum orta sahasına aktararak hücum bağlantısını kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Assists", "Touches", "Turn Over"]},
        ],
        "Final Third": [
            {"name": "Finishing Forward", "description": "Ceza sahasında şut kalitesi, gol üretimi ve bitiricilik verimliliğiyle öne çıkar.", "metrics": ["Goals", "Expected Goals", "Expected Goals On Target", "Shooting Performance", "Goal Conversion (%)", "On-Target to Goal Conversion (%)", "Shots On Target (%)", "Shot Quality (%)", "Aerials Won", "Aerials Won (%)", "Big Chances Missed"]},
            {"name": "Service Forward", "description": "Sonlandırıcı rolün yanında takım arkadaşlarına pozisyon hazırlayan bağlantı oyuncusu olur.", "metrics": ["Assists", "Key Passes", "Assist Efficiency (%)", "Big Chances Created", "Accurate Passes (%)", "Through Balls", "Through Balls Won", "Aerials Won", "Duels Won", "Fouls Drawn"]},
        ],
        "High Block": [
            {"name": "Front-Line Presser", "description": "Rakip stoper ve kaleciye direkt baskı uygular.", "metrics": ["Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Ball Recovery", "Fouls", "Total Duels"]},
            {"name": "Passing-Lane Blocker", "description": "Rakibin merkez ve stoper bağlantılarını pas açılarını kapatarak sınırlar.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won", "Tackles"]},
        ],
        "Mid Block": [
            {"name": "Holding-Midfield Blocker", "description": "Rakibin stoper ile ön libero arasındaki pas bağlantısını kapatır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Total Duels", "Duels Won"]},
            {"name": "Transition Threat", "description": "Takım topu kazandığında hızlı hücum için ileri pozisyonunu korur.", "metrics": ["Offsides", "Shots Total", "Fouls Drawn", "Touches", "Dispossessed", "Possession Lost"]},
            {"name": "Physical Front-Line Defender", "description": "Merkezde fiziksel mücadeleyle rakibin rahat ilerlemesini engeller.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Aerials Won", "Fouls", "Fouls Drawn"]},
        ],
        "Low Block": [
            {"name": "Counter-Outlet Forward", "description": "Takım savunmadan çıktığında ilk hedef oyuncu olur.", "metrics": ["Aerials Won", "Duels Won", "Fouls Drawn", "Accurate Passes (%)", "Possession Lost", "Touches"]},
            {"name": "Set-Piece Defender", "description": "Ceza sahası savunmasında hava toplarına ve uzaklaştırmalara destek verir.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Blocked Shots", "Ball Recovery"]},
            {"name": "Hold-Up Outlet Forward", "description": "Top kazanıldığında ilk pası veya uzun topu koruyarak takımın bloktan çıkmasını sağlar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Total Duels", "Fouls Drawn", "Dispossessed", "Turn Over"]},
        ],
    },
    "WIDE": {
        "Build-up": [
            {"name": "Width-Holding Winger", "description": "Çizgi genişliğini koruyarak ilk bölgeden pasla çıkışa yardım eder.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Total Crosses", "Backward Passes"]},
            {"name": "Link-Up Winger", "description": "Bek ve merkez oyuncularıyla güvenli pas bağlantıları oluşturur.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Touches", "Possession Lost", "Turn Over"]},
        ],
        "Progression": [
            {"name": "One-v-One Winger", "description": "Rakip oyuncuları dripling ile geçerek savunma dengesini bozar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed", "Possession Lost"]},
            {"name": "Progressive Winger", "description": "Topu pas veya dripling yoluyla final bölgesine taşır.", "metrics": ["Passes In Final Third", "Successful Dribbles", "Accurate Passes", "Accurate Passes (%)", "Touches", "Possession Lost"]},
            {"name": "Combination Winger", "description": "Bek, orta saha ve hücum oyuncularıyla kısa pas bağlantıları kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Through Balls", "Through Balls Won", "Assists"]},
        ],
        "Final Third": [
            {"name": "Touchline Creator", "description": "Kanattan orta ve servis üretimine odaklanır.", "metrics": ["Total Crosses", "Accurate Crosses", "Successful Crosses (%)", "Assists", "Big Chances Created", "Assist Efficiency (%)"]},
            {"name": "Inside Forward", "description": "İçe kat ederek şut ve gol tehdidi oluşturur.", "metrics": ["Shots Total", "Shots On Target", "Shots On Target (%)", "Expected Goals", "Shooting Performance", "Goal Conversion (%)", "Goals"]},
            {"name": "Creative Winger", "description": "Son pas, kilit pas ve büyük şans yaratımıyla öne çıkar.", "metrics": ["Key Passes", "Assists", "Assist Efficiency (%)", "Big Chances Created", "Through Balls", "Through Balls Won", "Passes In Final Third"]},
        ],
        "High Block": [
            {"name": "Full-Back Presser", "description": "Rakip beke doğrudan baskı uygular.", "metrics": ["Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Ball Recovery", "Fouls"]},
            {"name": "Wide Passing-Lane Blocker", "description": "Rakibin bek ve kanat arasındaki pas bağlantısını sınırlar.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won", "Tackles"]},
            {"name": "High Ball-Winning Winger", "description": "Rakip yarı sahada top kazanıp hızlı hücum üretir.", "metrics": ["Ball Recovery", "Interceptions", "Key Passes", "Assists", "Shots Total", "Big Chances Created"]},
        ],
        "Mid Block": [
            {"name": "Tracking Winger", "description": "Rakip bek ve kanat koşularını takip ederek orta blok bütünlüğünü korur.", "metrics": ["Tackles", "Interceptions", "Ball Recovery", "Total Duels", "Duels Won", "Dribbled Past", "Clearances"]},
            {"name": "Channel Defender", "description": "Kanat koridorunu kapatıp rakibin çizgiden ilerlemesini sınırlar.", "metrics": ["Tackles Won", "Interceptions", "Blocked Shots", "Clearances", "Dribbled Past", "Ball Recovery"]},
            {"name": "Transition Winger", "description": "Top kazanıldığında hızlı şekilde hücuma çıkar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Passes In Final Third", "Key Passes", "Fouls Drawn", "Possession Lost"]},
        ],
        "Low Block": [
            {"name": "Full-Back Supporter", "description": "Kendi bekine ikili savunmada destek verir.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Total Duels", "Duels Won", "Dribbled Past"]},
            {"name": "Back-Post Defender", "description": "Ters kanattan gelen ortalarda arka direği savunur.", "metrics": ["Aerials", "Aerials Won", "Clearances", "Blocked Shots", "Ball Recovery"]},
            {"name": "Counter Carrier", "description": "Top kazanıldığında dripling veya ileri pasla kontrayı başlatır.", "metrics": ["Successful Dribbles", "Dribble Accuracy (%)", "Accurate Passes (%)", "Passes In Final Third", "Fouls Drawn", "Dispossessed"]},
        ],
    },
    "CAM": {
        "Build-up": [
            {"name": "Between-Lines Connector", "description": "Savunma ve orta saha hatları arasında pas bağlantısı oluşturur.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Turn Over", "Possession Lost"]},
            {"name": "Drifting Playmaker", "description": "Kanada açılarak genişlik sağlar ve ilk baskı hattından çıkışa yardım eder.", "metrics": ["Long Balls Won", "Aerials", "Aerials Won", "Aerials Won (%)", "Passes", "Accurate Passes", "Accurate Passes (%)", "Touches", "Fouls Drawn"]},
        ],
        "Progression": [
            {"name": "Between-Lines Playmaker", "description": "Rakip orta saha ve savunma hattı arasında top alır.", "metrics": ["Touches", "Passes In Final Third", "Key Passes", "Fouls Drawn", "Dispossessed", "Accurate Passes (%)"]},
            {"name": "Through-Ball Specialist", "description": "Savunma arkasına ve dar kanallara etkili paslar verir.", "metrics": ["Through Balls", "Through Balls Won", "Key Passes", "Big Chances Created", "Assists", "Assist Efficiency (%)"]},
            {"name": "Central Dribbler", "description": "Top sürerek rakip orta saha hattını aşar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed", "Possession Lost"]},
        ],
        "Final Third": [
            {"name": "Final-Pass Creator", "description": "Kilit pas ve asist üretimiyle hücumu tamamlar.", "metrics": ["Assists", "Key Passes", "Big Chances Created", "Assist Efficiency (%)", "Through Balls Won", "Passes In Final Third"]},
            {"name": "Second Forward", "description": "Ceza sahasına koşular yaparak gol tehdidi oluşturur.", "metrics": ["Goals", "Expected Goals", "Shots Total", "Shots On Target", "Goal Conversion (%)", "Shot Quality (%)"]},
            {"name": "Shooting Threat", "description": "Ceza sahası çevresinden düzenli şut üretir.", "metrics": ["Shots Total", "Shots On Target", "Shots On Target (%)", "Shooting Performance", "Shot Quality (%)", "Goals"]},
        ],
        "High Block": [
            {"name": "Pivot-and-Center-Back Presser", "description": "Rakip ön libero veya stoperlere baskı yaparak merkezden oyun kurmayı bozar.", "metrics": ["Interceptions", "Tackles", "Tackles Won", "Ball Recovery", "Fouls", "Total Duels"]},
            {"name": "Second-Ball Collector", "description": "Ön alan baskısının arkasındaki seken topları kazanır.", "metrics": ["Ball Recovery", "Interceptions", "Total Duels", "Duels Won", "Touches"]},
            {"name": "Ball-Winning Creator", "description": "Önde kazanılan toplardan hızlı şekilde fırsat yaratır.", "metrics": ["Key Passes", "Big Chances Created", "Assists", "Shots Total", "Goals", "Ball Recovery"]},
        ],
        "Mid Block": [
            {"name": "Central Passing-Lane Blocker", "description": "Rakibin merkezden hat kıran paslarını sınırlar.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won"]},
            {"name": "Press Trigger", "description": "Hatalı pas veya geri paslarda baskıyı başlatır.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Ball Recovery", "Fouls"]},
            {"name": "Transition Playmaker", "description": "Top kazanıldığında hücumcuları hızlı şekilde oyuna sokar.", "metrics": ["Key Passes", "Through Balls", "Through Balls Won", "Passes In Final Third", "Assists"]},
        ],
        "Low Block": [
            {"name": "Box-Edge Defender", "description": "Merkezde şut ve ara pas alanlarını kapatır.", "metrics": ["Interceptions", "Tackles", "Blocked Shots", "Ball Recovery", "Total Duels"]},
            {"name": "Second-Ball Playmaker", "description": "Savunmadan uzaklaştırılan topları kazanıp oyunu yeniden başlatır.", "metrics": ["Ball Recovery", "Interceptions", "Touches", "Duels Won", "Accurate Passes (%)", "Possession Lost"]},
            {"name": "Counter Initiator", "description": "Top kazanıldığında ilk yaratıcı pası veya driplingi yapar.", "metrics": ["Accurate Passes (%)", "Key Passes", "Through Balls Won", "Successful Dribbles", "Fouls Drawn", "Turn Over"]},
        ],
    },
    "CM": {
        "Build-up": [
            {"name": "First-Pass Player", "description": "Stoperlerden top alarak takımın oyun kurulumunu başlatır.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Possession Lost"]},
            {"name": "Press-Breaking Passer", "description": "Baskı hattının arkasına pas atarak oyunu ilerletir.", "metrics": ["Accurate Passes", "Accurate Passes (%)", "Long Balls", "Long Balls Won", "Through Balls Won", "Turn Over"]},
            {"name": "Central Ball Carrier", "description": "Dripling ile topu birinci bölgeden ikinci bölgeye taşır.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Dispossessed", "Fouls Drawn", "Possession Lost"]},
        ],
        "Progression": [
            {"name": "Vertical Passer", "description": "Merkezden final bölgesine hat kıran paslar verir.", "metrics": ["Passes In Final Third", "Key Passes", "Through Balls", "Through Balls Won", "Accurate Passes (%)"]},
            {"name": "Play Director", "description": "Uzun paslarla takımın hücum yönünü değiştirir.", "metrics": ["Long Balls", "Long Balls Won", "Long Balls Won (%)", "Passes", "Accurate Passes"]},
            {"name": "Central Ball Carrier", "description": "Top sürerek rakip orta saha hattını geçer.", "metrics": ["Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed", "Possession Lost"]},
        ],
        "Final Third": [
            {"name": "Front-Line Connector", "description": "Ceza sahası çevresinde pas dolaşımını sürdürür.", "metrics": ["Passes In Final Third", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Touches"]},
            {"name": "Box-Arriving Midfielder", "description": "İkinci dalga koşularıyla gol pozisyonuna girer.", "metrics": ["Goals", "Expected Goals", "Shots Total", "Shots On Target", "Goal Conversion (%)", "Shot Quality (%)"]},
            {"name": "Attack-Sustaining Player", "description": "İkinci topları kazanarak hücumu yeniden başlatır.", "metrics": ["Ball Recovery", "Interceptions", "Shots Total", "Passes In Final Third", "Possession Lost"]},
        ],
        "High Block": [
            {"name": "Pivot-and-Center-Back Presser", "description": "Rakibin ön libero veya stoperlerine öne çıkarak baskı yapar.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Ball Recovery", "Total Duels", "Fouls"]},
            {"name": "Second-Ball Winner", "description": "Pres sonrası seken topları kazanır.", "metrics": ["Ball Recovery", "Duels Won", "Aerials Won", "Interceptions", "Touches"]},
            {"name": "Counter-Presser", "description": "Top kaybından sonra ilk baskıyı yapar.", "metrics": ["Tackles", "Tackles Won", "Ball Recovery", "Fouls", "Dribbled Past"]},
        ],
        "Mid Block": [
            {"name": "Passing-Lane Blocker", "description": "Rakibin merkez ve hatlar arasındaki pas bağlantılarını kapatarak oyunun yönünü belirler. Doğrudan topa gitmekten çok doğru pozisyon alarak rakibin ilerlemesini zorlaştırır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Accurate Passes (%)"]},
            {"name": "Space Protector", "description": "Takımın savunma blok bütünlüğünü korur. Boş alanları kapatır, rakibin merkezden ilerlemesini zorlaştırır ve savunma hattını destekler.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Blocked Shots", "Clearances", "Dribbled Past"]},
            {"name": "Duel Specialist", "description": "Rakip oyuncularla bire bir mücadelelere girmeyi tercih eder. Fiziksel üstünlüğüyle top kazanır ve rakibin hücum devamlılığını bozar.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Tackles", "Tackles Won", "Tackles Won (%)", "Aerials Won", "Aerials Won (%)", "Fouls"]},
        ],
        "Low Block": [
            {"name": "Box-Edge Protector", "description": "Ceza sahası önündeki merkez alanı savunur.", "metrics": ["Interceptions", "Tackles", "Blocked Shots", "Ball Recovery", "Total Duels"]},
            {"name": "Second-Ball Winner", "description": "Uzaklaştırılan topların tekrar rakibe geçmesini önler.", "metrics": ["Ball Recovery", "Aerials Won", "Duels Won", "Clearances", "Touches"]},
            {"name": "Transition Stopper", "description": "Ceza sahası çevresinde top kazanıldıktan sonra rakibin ikinci hücumunu engeller, ikinci baskıyı kırar ve savunma dengesini korur.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Duels Won", "Total Duels", "Clearances"]},
        ],
    },
    "CDM": {
        "Build-up": [
            {"name": "Defensive Link Player", "description": "Stoperlerle orta saha arasında pas istasyonu olur.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Possession Lost"]},
            {"name": "First-Pass Specialist", "description": "Baskı altında oyunu güvenli ve doğru paslarla başlatır.", "metrics": ["Accurate Passes (%)", "Long Balls", "Long Balls Won", "Long Balls Won (%)", "Turn Over", "Error Lead To Shot"]},
            {"name": "Switch-of-Play Player", "description": "Uzun paslarla baskıyı ters kanada taşır.", "metrics": ["Long Balls", "Long Balls Won", "Long Balls Won (%)", "Passes", "Accurate Passes"]},
        ],
        "Progression": [
            {"name": "Vertical Playmaker", "description": "Merkezden hücum hattına dikey paslar verir.", "metrics": ["Passes In Final Third", "Through Balls", "Through Balls Won", "Accurate Passes (%)", "Key Passes"]},
            {"name": "Tempo Controller", "description": "Pas hacmi ve top kullanımıyla oyunun ritmini belirler.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Touches", "Possession Lost"]},
            {"name": "Balance Player", "description": "Takım ilerlerken top kaybına karşı savunma güvenliği sağlar.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Total Duels", "Duels Won"]},
        ],
        "Final Third": [
            {"name": "Rest-Defense Player", "description": "Takım hücum ederken kontra hücumlara karşı pozisyon alır.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Tackles Won", "Fouls"]},
            {"name": "Scoring Final-Third Contributor", "description": "Final bölgede hücuma katılarak gol ve asist katkısı üretir.", "metrics": ["Goals", "Assists", "Expected Goals", "Shots Total", "Shots On Target", "Shots On Target (%)", "Goal Conversion (%)", "Key Passes", "Big Chances Created", "Assist Efficiency (%)"]},
            {"name": "Attack Redirector", "description": "Geri dönen toplarla hücum yönünü yeniden kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Long Balls Won", "Passes In Final Third"]},
        ],
        "High Block": [
            {"name": "Press-Cover Player", "description": "Ön alan baskısının arkasındaki boşluğu korur.", "metrics": ["Ball Recovery", "Interceptions", "Aerials Won", "Duels Won", "Touches"]},
            {"name": "Central Presser", "description": "Rakip orta saha oyuncusuna baskı uygular.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Total Duels", "Fouls"]},
            {"name": "Counter Breaker", "description": "Rakibin geçiş hücumunu ilk aşamada durdurur.", "metrics": ["Tackles", "Tackles Won", "Ball Recovery", "Fouls", "Yellow Cards", "Dribbled Past"]},
        ],
        "Mid Block": [
            {"name": "Passing-Lane Blocker", "description": "Rakibin merkez ve hatlar arasındaki pas bağlantılarını kapatarak oyunun yönünü belirler. Doğrudan topa gitmekten çok doğru pozisyon alarak rakibin ilerlemesini zorlaştırır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Accurate Passes (%)"]},
            {"name": "Space Protector", "description": "Takımın savunma blok bütünlüğünü korur. Boş alanları kapatır, rakibin merkezden ilerlemesini zorlaştırır ve savunma hattını destekler.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Blocked Shots", "Clearances", "Dribbled Past"]},
            {"name": "Duel Specialist", "description": "Rakip oyuncularla bire bir mücadelelere girmeyi tercih eder. Fiziksel üstünlüğüyle top kazanır ve rakibin hücum devamlılığını bozar.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Tackles", "Tackles Won", "Tackles Won (%)", "Aerials Won", "Aerials Won (%)", "Fouls"]},
        ],
        "Low Block": [
            {"name": "Box-Edge Sweeper", "description": "Ceza sahası önündeki şut ve pas alanlarını kapatır.", "metrics": ["Interceptions", "Tackles", "Blocked Shots", "Ball Recovery", "Clearances"]},
            {"name": "Box Second-Ball Controller", "description": "Ceza sahası içinde veya çevresinde seken topları kontrol ederek savunma dengesini korur.", "metrics": ["Ball Recovery", "Aerials Won", "Duels Won", "Clearances", "Touches"]},
            {"name": "Transition Stopper", "description": "Ceza sahası çevresinde top kazanıldıktan sonra rakibin ikinci hücumunu engeller, ikinci baskıyı kırar ve savunma dengesini korur.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Duels Won", "Total Duels", "Clearances"]},
        ],
    },
    "FB": {
        "Build-up": [
            {"name": "Wide Outlet Full-Back", "description": "Kanatta geniş pas seçeneği oluşturarak ilk bölgeden çıkışı sağlar.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes"]},
            {"name": "Safe-Passing Full-Back", "description": "Baskı altında düşük riskli ve doğru paslarla oyun kurulumunu sürdürür.", "metrics": ["Accurate Passes (%)", "Possession Lost", "Turn Over", "Dispossessed", "Error Lead To Shot", "Error Lead To Goal"]},
            {"name": "Ball-Carrying Full-Back", "description": "Dripling yoluyla ilk baskı hattını aşar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed"]},
        ],
        "Progression": [
            {"name": "Overlapping Full-Back", "description": "Kanat oyuncusunun dışından ileri koşular yapar.", "metrics": ["Touches", "Total Crosses", "Passes In Final Third", "Successful Dribbles", "Fouls Drawn"]},
            {"name": "Combination Full-Back", "description": "Kanat ve merkez oyuncularıyla pas bağlantıları kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Through Balls Won"]},
            {"name": "Progressive Full-Back", "description": "Topu pas veya dripling yoluyla final bölgesine taşır.", "metrics": ["Successful Dribbles", "Dribble Accuracy (%)", "Passes In Final Third", "Accurate Passes (%)", "Possession Lost"]},
        ],
        "Final Third": [
            {"name": "Crossing Full-Back", "description": "Kanattan ceza sahasına servis üretir.", "metrics": ["Total Crosses", "Accurate Crosses", "Successful Crosses (%)", "Assists", "Big Chances Created"]},
            {"name": "Back-Pass Outlet", "description": "Çizgiye inip ceza sahası çevresine gol fırsatı hazırlayan paslar verir.", "metrics": ["Key Passes", "Assists", "Assist Efficiency (%)", "Big Chances Created", "Accurate Passes (%)"]},
            {"name": "Inverted-Channel Full-Back", "description": "Yarı koridora girerek pas bağlantısı kurar ve merkezde sayısal üstünlük oluşturur.", "metrics": ["Passes In Final Third", "Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Through Balls", "Through Balls Won", "Touches", "Possession Lost"]},
        ],
        "High Block": [
            {"name": "High-Pressing Full-Back", "description": "Rakip kanat veya bek oyuncusuna önden baskı yapar.", "metrics": ["Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Ball Recovery"]},
            {"name": "Wide Ball-Winning Full-Back", "description": "Rakibi çizgiye sıkıştırıp topu geri kazanır.", "metrics": ["Ball Recovery", "Interceptions", "Total Duels", "Duels Won", "Fouls"]},
            {"name": "High-Line Defender", "description": "Savunma arkasına atılan topları karşılar.", "metrics": ["Interceptions", "Ball Recovery", "Clearances", "Aerials Won", "Dribbled Past"]},
        ],
        "Mid Block": [
            {"name": "Channel Defender", "description": "Rakibin kanattan ilerlemesini sınırlar.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Dribbled Past", "Total Duels", "Duels Won"]},
            {"name": "Passing-Lane Blocker", "description": "Bek, kanat ve merkez arasındaki pas bağlantılarını kapatır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Clearances", "Dribbled Past"]},
            {"name": "Run Tracker", "description": "Rakip kanat oyuncusunun savunma arkasına koşularını takip eder.", "metrics": ["Interceptions", "Clearances", "Ball Recovery", "Aerials Won", "Fouls"]},
        ],
        "Low Block": [
            {"name": "Back-Post Defender", "description": "Ters kanattan gelen ortalarda arka direği korur.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Blocked Shots"]},
            {"name": "Box One-v-One Defender", "description": "Ceza sahası içinde rakip kanat oyuncularıyla bire bir savunma yapar.", "metrics": ["Tackles", "Tackles Won", "Total Duels", "Duels Won", "Dribbled Past", "Fouls"]},
            {"name": "Danger Clearer", "description": "Ceza sahası içindeki topları güvenli bölgeye uzaklaştırır.", "metrics": ["Clearances", "Clearance Offline", "Blocked Shots", "Ball Recovery", "Error Lead To Goal", "Own Goals"]},
        ],
    },
    "CB": {
        "Build-up": [
            {"name": "Safe Playmaking Center-Back", "description": "Savunmadan doğru ve güvenli paslarla oyunu başlatır.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Touches", "Possession Lost", "Error Lead To Goal"]},
            {"name": "Line-Breaking Center-Back", "description": "Dikey ve uzun paslarla rakibin ilk baskı hattını aşar.", "metrics": ["Passes In Final Third", "Long Balls", "Long Balls Won", "Long Balls Won (%)", "Through Balls Won"]},
            {"name": "Ball-Carrying Center-Back", "description": "Rakip baskı yapmadığında dripling ile alan kazanır.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Dispossessed", "Turn Over"]},
        ],
        "Progression": [
            {"name": "Vertical-Passing Center-Back", "description": "Topu doğrudan orta saha veya hücum hattına aktarır.", "metrics": ["Passes In Final Third", "Long Balls Won", "Through Balls", "Through Balls Won", "Accurate Passes (%)"]},
            {"name": "Switching Center-Back", "description": "Uzun paslarla hücum yönünü değiştirir.", "metrics": ["Long Balls", "Long Balls Won", "Long Balls Won (%)", "Passes", "Accurate Passes"]},
            {"name": "Defensive-Balance Center-Back", "description": "Takım ilerlerken geride alan ve geçiş güvenliği sağlar.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Total Duels", "Duels Won"]},
        ],
        "Final Third": [
            {"name": "Set-Piece Threat", "description": "Korner ve serbest vuruşlarda hava topu tehdidi oluşturur.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Shots Total", "Goals"]},
            {"name": "Rest-Attack Collector", "description": "Rakibin uzaklaştırdığı topları yeniden kazanır.", "metrics": ["Ball Recovery", "Interceptions", "Aerials Won", "Touches", "Passes"]},
            {"name": "Counter Stopper", "description": "Top kaybında rakibin hızlı hücumunu erken keser.", "metrics": ["Interceptions", "Tackles", "Last Man Tackle", "Ball Recovery", "Fouls", "Yellow Cards"]},
        ],
        "High Block": [
            {"name": "High-Line Center-Back", "description": "Savunma çizgisini önde tutarak takım boyunu kısaltır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won"]},
            {"name": "Long-Ball Defender", "description": "Rakibin baskıdan çıkmak için kullandığı uzun topları karşılar.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Duels Won"]},
            {"name": "Depth Defender", "description": "Savunma arkasındaki koşuları ve boş alanı kontrol eder.", "metrics": ["Interceptions", "Last Man Tackle", "Clearances", "Ball Recovery", "Fouls", "Error Lead To Goal"]},
        ],
        "Mid Block": [
            {"name": "Forward Marker", "description": "Rakip santraforla fiziksel mücadele ederek top almasını engeller.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Aerials Won", "Fouls", "Fouls Drawn"]},
            {"name": "Through-Ball Defender", "description": "Savunma arkasına atılan pasları keser.", "metrics": ["Interceptions", "Ball Recovery", "Clearances", "Last Man Tackle", "Error Lead To Shot"]},
            {"name": "Defensive Organizer", "description": "Pozisyonunu koruyarak savunma hattının dengesini sağlar.", "metrics": ["Interceptions", "Clearances", "Blocked Shots", "Goals Conceded", "Error Lead To Goal", "Rating"]},
        ],
        "Low Block": [
            {"name": "Box Defender", "description": "Ceza sahası içinde şut, pas ve fiziksel mücadelelere müdahale eder.", "metrics": ["Clearances", "Blocked Shots", "Interceptions", "Tackles", "Ball Recovery"]},
            {"name": "Aerial Dominator", "description": "Ortaları ve duran topları hava mücadelesiyle uzaklaştırır.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Clearance Offline"]},
            {"name": "Last-Action Center-Back", "description": "Kaleye giden şutlara veya son oyuncu koşularına kritik müdahaleler yapar.", "metrics": ["Last Man Tackle", "Blocked Shots", "Clearance Offline", "Clearances", "Error Lead To Goal", "Own Goals"]},
        ],
    },
}

NEGATIVE_METRIC_RANGES: Dict[str, Tuple[float, float]] = {
    "Goals Conceded": (0, 2),
    "Penalties Committed": (0, 0.15),
    "Penalties Missed": (0, 0.15),
    "Shots Off Target": (0, 2.5),
    "Big Chances Missed": (0, 1),
    "Aerials Lost": (0, 4),
    "Duels Lost": (0, 6),
    "Fouls": (0, 2),
    "Dispossessed": (0, 5),
    "Dribbled Past": (0, 2),
    "Turn Over": (0, 3),
    "Possession Lost": (0, 20),
    "Offsides": (0, 0.3),
    "Own Goals": (0, 0.2),
    "Error Lead To Goal": (0, 0.25),
    "Error Lead To Shot": (0, 0.4),
    "Yellow Cards": (0, 0.4),
    "Yellow & Red Cards": (0, 1),
    "Red Cards": (0, 0.2),
}

PHASE_METRIC_RANGES: Dict[str, Tuple[float, float]] = {
    "Blocked Shots": (0, 0.5),
    "Tackles Won": (0, 1.5),
    "Big Chances Missed": (0, 1),
    "Goals Conceded": (0, 2),
    "Long Balls Won": (0, 2),
    "Successful Crosses (%)": (0, 100),
    "Last Man Tackle": (0, 0.3),
    "Accurate Passes (%)": (0, 100),
    "Aerials Won (%)": (0, 100),
    "Fouls": (0, 2),
    "Hit Woodwork": (0, 0.15),
    "Total Duels": (0, 9),
    "Accurate Passes": (0, 50),
    "Error Lead To Goal": (0, 0.25),
    "Error Lead To Shot": (0, 0.4),
    "Key Passes": (0, 2),
    "Penalties Missed": (0, 0.15),
    "Yellow Cards": (0, 0.4),
    "Duels Won": (0, 6.5),
    "Rating": (0, 10),
    "Shots Total": (0, 2.5),
    "Shots On Target (%)": (0, 100),
    "Expected Goals": (0, 0.6),
    "Expected Goals On Target": (0, 0.4),
    "Shooting Performance": (-0.4, 0.4),
    "Shot Quality (%)": (0, 40),
    "On-Target Shot Quality (%)": (0, 100),
    "Goal Conversion (%)": (0, 40),
    "On-Target to Goal Conversion (%)": (0, 40),
    "Assist Efficiency (%)": (0, 25),
    "Dribble Accuracy (%)": (0, 75),
    "Total Crosses": (0, 3),
    "Passes": (0, 50),
    "Offsides": (0, 0.3),
    "Aerials Lost": (0, 4),
    "Penalties Committed": (0, 0.15),
    "Possession Lost": (0, 20),
    "Long Balls": (0, 4),
    "Aerials Won": (0, 4),
    "Clearances": (0, 1),
    "Man Of Match": (0, 0.15),
    "Match Count": (0, 35),
    "Ball Recovery": (0, 3),
    "Red Cards": (0, 0.2),
    "Accurate Crosses": (0, 1.5),
    "Goals": (0, 0.4),
    "Offsides Provoked": (0, 0.5),
    "Aerials": (0, 5),
    "Saves": (0, 4),
    "Touches": (0, 50),
    "Assists": (0, 0.3),
    "Minutes Played": (0, 90),
    "Dribble Attempts": (0, 2),
    "Tackles": (0, 3),
    "Turn Over": (0, 3),
    "Fouls Drawn": (0, 2),
    "Big Chances Created": (0, 0.75),
    "Long Balls Won (%)": (0, 100),
    "Penalties Scored": (0, 0.15),
    "Penalties Won": (0, 0.1),
    "Duels Lost": (0, 6),
    "Penalties Saved": (0, 0.5),
    "Saves Insidebox": (0, 4),
    "Shots Off Target": (0, 2.5),
    "Good High Claim": (0, 1.5),
    "Dispossessed": (0, 5),
    "Shots On Target": (0, 1.25),
    "Through Balls Won": (0, 0.25),
    "Duels Won (%)": (0, 100),
    "Punches": (0, 0.75),
    "Successful Dribbles": (0, 2),
    "Tackles Won (%)": (0, 100),
    "Interceptions": (0, 2),
    "Yellow & Red Cards": (0, 1),
    "Backward Passes": (0, 10),
    "Captain": (0, 0.5),
    "Own Goals": (0, 0.2),
    "Dribbled Past": (0, 2),
    "Clearance Offline": (0, 0.05),
    "Through Balls": (0, 0.5),
    "Passes In Final Third": (0, 6),
}

CONCERN_RISK_THRESHOLD = 0.33
WATCH_RISK_THRESHOLD = 0.66

CATEGORY_PERSPECTIVE_METRICS: Dict[str, List[str]] = {
    "Contribution & Impact": [
        "Minutes Played",
        "Penalties Won",
        "Touches",
        "Big Chances Created",
        "Dribble Attempts",
        "Successful Dribbles",
        "Dribble Accuracy (%)",
        "Man Of Match",
        "Rating",
        "Captain",
        "Fouls Drawn",
        "Offsides Provoked",
    ],
    "Shooting & Finishing": [
        "Shots Total",
        "Shots On Target",
        "Shots On Target (%)",
        "Expected Goals",
        "Expected Goals On Target",
        "Shooting Performance",
        "Shot Quality (%)",
        "On-Target Shot Quality (%)",
        "Goal Conversion (%)",
        "On-Target to Goal Conversion (%)",
        "Goals",
        "Hit Woodwork",
        "Penalties Scored",
    ],
    "Passing & Distribution": [
        "Assists",
        "Assist Efficiency (%)",
        "Long Balls",
        "Long Balls Won",
        "Long Balls Won (%)",
        "Total Crosses",
        "Accurate Crosses",
        "Successful Crosses (%)",
        "Passes",
        "Accurate Passes",
        "Accurate Passes (%)",
        "Backward Passes",
        "Key Passes",
        "Passes In Final Third",
        "Through Balls",
        "Through Balls Won",
    ],
    "Defending": [
        "Interceptions",
        "Tackles",
        "Tackles Won",
        "Tackles Won (%)",
        "Ball Recovery",
        "Duels Won",
        "Duels Won (%)",
        "Total Duels",
        "Aerials",
        "Aerials Won",
        "Aerials Won (%)",
        "Clearances",
        "Blocked Shots",
        "Shots Blocked",
        "Last Man Tackle",
        "Clearance Offline",
    ],
    "Goalkeeping": [
        "Saves",
        "Saves Insidebox",
        "Penalties Saved",
        "Punches",
        "Good High Claim",
        "Goalkeeper Goals Conceded",
        "Goals Conceded",
        "Long Balls",
        "Long Balls Won",
        "Long Balls Won (%)",
        "Accurate Passes",
        "Accurate Passes (%)",
        "Possession Lost",
    ],
    "Errors & Discipline": [
        "Goals Conceded",
        "Penalties Committed",
        "Penalties Missed",
        "Shots Off Target",
        "Big Chances Missed",
        "Aerials Lost",
        "Duels Lost",
        "Fouls",
        "Dispossessed",
        "Dribbled Past",
        "Turn Over",
        "Possession Lost",
        "Offsides",
        "Own Goals",
        "Error Lead To Goal",
        "Error Lead To Shot",
        "Yellow Cards",
        "Yellow & Red Cards",
        "Red Cards",
    ],
}

PHASE_FIT_METRICS: Dict[str, List[str]] = {
    "Build-up": [
        "Passes",
        "Accurate Passes",
        "Accurate Passes (%)",
        "Backward Passes",
        "Touches",
        "Possession Lost",
        "Turn Over",
    ],
    "Progression": [
        "Passes In Final Third",
        "Through Balls",
        "Through Balls Won",
        "Long Balls",
        "Long Balls Won",
        "Long Balls Won (%)",
        "Dribble Attempts",
        "Successful Dribbles",
        "Dribble Accuracy (%)",
        "Fouls Drawn",
    ],
    "Final Third": [
        "Assists",
        "Assist Efficiency (%)",
        "Key Passes",
        "Big Chances Created",
        "Total Crosses",
        "Accurate Crosses",
        "Successful Crosses (%)",
        "Shots Total",
        "Shots On Target",
        "Shot Quality (%)",
        "On-Target Shot Quality (%)",
        "Goal Conversion (%)",
        "On-Target to Goal Conversion (%)",
        "Goals",
    ],
    "High Block": [
        "Tackles",
        "Tackles Won",
        "Tackles Won (%)",
        "Interceptions",
        "Ball Recovery",
        "Fouls",
        "Dribbled Past",
        "Duels Won",
        "Duels Lost",
    ],
    "Mid Block": [
        "Interceptions",
        "Ball Recovery",
        "Duels Won",
        "Duels Won (%)",
        "Total Duels",
        "Tackles",
        "Tackles Won",
        "Clearances",
    ],
    "Low Block": [
        "Clearances",
        "Blocked Shots",
        "Shots Blocked",
        "Aerials",
        "Aerials Won",
        "Aerials Won (%)",
        "Last Man Tackle",
        "Clearance Offline",
        "Error Lead To Shot",
        "Error Lead To Goal",
    ],
}

GK_PHASE_FIT_METRICS: Dict[str, List[str]] = {
    "Build-up": [
        "Passes",
        "Accurate Passes",
        "Accurate Passes (%)",
        "Backward Passes",
        "Touches",
        "Long Balls",
        "Long Balls Won",
        "Long Balls Won (%)",
        "Possession Lost",
        "Turn Over",
    ],
    "Low Block": [
        "Saves",
        "Saves Insidebox",
        "Penalties Saved",
        "Punches",
        "Good High Claim",
        "Goalkeeper Goals Conceded",
        "Goals Conceded",
        "Error Lead To Shot",
        "Error Lead To Goal",
    ],
}


def _role_short(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in ROLE_SHORT_TO_LONG:
        return upper
    return ROLE_LONG_TO_SHORT.get(raw.lower())


def _normalized_position_counts(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: Dict[str, int] = {}
    for raw_role, raw_count in value.items():
        short = _role_short(raw_role)
        if not short:
            continue
        try:
            count = int(float(raw_count))
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[short] = counts.get(short, 0) + count
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _is_goalkeeper_card(player_card: Dict[str, Any]) -> bool:
    counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    raw_roles: List[Any] = list(counts.keys())
    roles_value = (player_card or {}).get("roles")
    if isinstance(roles_value, list):
        raw_roles.extend(roles_value)
    raw_roles.extend(
        [
            (player_card or {}).get("primary_position_code"),
            (player_card or {}).get("role"),
            (player_card or {}).get("position_name"),
            (player_card or {}).get("position"),
        ]
    )
    return any(_role_short(role) == "GK" for role in raw_roles)


def _top_position_roles(player_card: Dict[str, Any], limit: int = 2) -> List[str]:
    counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    if counts:
        return list(counts.keys())[:limit]

    raw_roles: List[Any] = []
    raw_roles.extend(
        [
            (player_card or {}).get("primary_position_code"),
            (player_card or {}).get("primaryPositionCode"),
            (player_card or {}).get("position_name"),
            (player_card or {}).get("position"),
            (player_card or {}).get("role"),
        ]
    )
    roles_value = (player_card or {}).get("roles")
    if isinstance(roles_value, list):
        raw_roles.extend(roles_value)
    mapped: List[str] = []
    for role in raw_roles:
        short = _role_short(role)
        if short and short not in mapped:
            mapped.append(short)
        if len(mapped) >= limit:
            break
    return mapped


def _required_phase_names(player_card: Dict[str, Any]) -> List[str]:
    if _is_goalkeeper_card(player_card or {}):
        return GOALKEEPER_PHASES
    return OUTFIELD_PHASES


def _phase_role_family(player_card: Dict[str, Any]) -> str:
    if _is_goalkeeper_card(player_card or {}):
        return "goalkeeper"
    top_roles = _top_position_roles(player_card or {}, 2)
    primary_role = top_roles[0] if top_roles else None
    if primary_role in ATTACK_LINE_PHASE_ROLES:
        return "front"
    if primary_role in CENTER_LINE_PHASE_ROLES:
        return "center"
    if primary_role in BACK_LINE_PHASE_ROLES:
        return "back"
    return "outfield"


def _taxonomy_key_for_role(role: str) -> Optional[str]:
    if role == "GK":
        return "GK"
    if role in WIDE_PHASE_ROLES:
        return "WIDE"
    if role in FULLBACK_PHASE_ROLES:
        return "FB"
    if role in {"CF", "CAM", "CM", "CDM", "CB"}:
        return role
    return None


def _phase_taxonomy_roles(player_card: Dict[str, Any]) -> List[Tuple[str, float]]:
    counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    if counts:
        ordered = list(counts.items())[:2]
        total_all = sum(counts.values()) or 0
        if len(ordered) == 1 or total_all <= 0:
            return [(ordered[0][0], 100.0)] if ordered else []
        first_role, first_count = ordered[0]
        second_role, second_count = ordered[1]
        first_pct = (first_count / total_all) * 100
        second_pct = (second_count / total_all) * 100
        selected = [(first_role, first_count)]
        if first_pct - second_pct <= 20:
            selected.append((second_role, second_count))
        selected_total = sum(count for _, count in selected) or 1
        return [(role, (count / selected_total) * 100) for role, count in selected]

    roles = _top_position_roles(player_card or {}, 2)
    return [(roles[0], 100.0)] if roles else []


def _metric_context_value(metric_docs: List[Dict[str, Any]], metric_name: str) -> Optional[Any]:
    selected: Optional[Any] = _derived_metric_value(metric_name, metric_docs)
    if selected not in (None, ""):
        return selected
    for doc in metric_docs or []:
        selected = _metric_value_from_metadata(doc.get("metadata") or {}, metric_name)
        if selected not in (None, ""):
            return selected
    return None


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _metric_signal_strength(metric_docs: List[Dict[str, Any]], metric_name: str) -> Optional[float]:
    value = _to_float(_metric_context_value(metric_docs, metric_name))
    metric_range = PHASE_METRIC_RANGES.get(metric_name) or NEGATIVE_METRIC_RANGES.get(metric_name)
    if value is None or not metric_range:
        return None

    minimum, maximum = metric_range
    if maximum <= minimum:
        return None
    normalized = _clamp((value - minimum) / (maximum - minimum))
    if metric_name in NEGATIVE_METRIC_RANGES:
        normalized = 1 - normalized
    return normalized


def _phase_category_score(category: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> float:
    strengths: List[float] = []
    for metric in category.get("metrics") or []:
        strength = _metric_signal_strength(metric_docs, metric)
        if strength is not None:
            strengths.append(strength)
    if not strengths:
        return 1.0

    strengths.sort(reverse=True)
    strongest = strengths[:5]
    average_strength = sum(strongest) / len(strongest)
    return 0.25 + average_strength


def _normalize_phase_distribution_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged_items: Dict[str, Dict[str, Any]] = {}
    for item in items:
        category_name = str(item.get("category") or "")
        if not category_name:
            continue
        existing = merged_items.get(category_name)
        if not existing:
            merged_items[category_name] = {
                **item,
                "source_roles": [item.get("source_role")],
                "metrics": list(item.get("metrics") or []),
                "available_metrics": list(item.get("available_metrics") or []),
            }
            continue

        existing["percentage"] = float(existing.get("percentage") or 0) + float(item.get("percentage") or 0)
        existing["score"] = max(float(existing.get("score") or 0), float(item.get("score") or 0))
        source_role = item.get("source_role")
        if source_role and source_role not in existing["source_roles"]:
            existing["source_roles"].append(source_role)
        for metric in item.get("metrics") or []:
            if metric not in existing["metrics"]:
                existing["metrics"].append(metric)
        for metric_value in item.get("available_metrics") or []:
            if metric_value not in existing["available_metrics"]:
                existing["available_metrics"].append(metric_value)
        existing["source_role"] = "/".join(str(role) for role in existing["source_roles"] if role)

    items = list(merged_items.values())
    total = sum(float(item["percentage"]) for item in items) or 1.0
    normalized: List[Dict[str, Any]] = []
    rounded_total = 0
    for index, item in enumerate(items):
        if index == len(items) - 1:
            percentage = max(0, 100 - rounded_total)
        else:
            percentage = int(round((float(item["percentage"]) / total) * 100))
            rounded_total += percentage
        normalized.append({**item, "percentage": percentage})
    return normalized


def _phase_taxonomy_distribution_for_role(
    role: str,
    metric_docs: List[Dict[str, Any]],
    phase: str,
) -> List[Dict[str, Any]]:
    taxonomy_key = _taxonomy_key_for_role(role)
    categories = (PHASE_ROLE_TAXONOMY.get(taxonomy_key or "") or {}).get(phase, [])
    if not categories:
        return []

    items: List[Dict[str, Any]] = []
    scores = [_phase_category_score(category, metric_docs) for category in categories]
    score_total = sum(scores) or float(len(categories)) or 1.0
    for category, score in zip(categories, scores):
        metrics = category.get("metrics") or []
        available_metrics: List[str] = []
        for metric in metrics:
            value = _metric_context_value(metric_docs, metric)
            if value not in (None, ""):
                available_metrics.append(f"{metric}={value}")
        items.append(
            {
                "source_role": role,
                "category": category["name"],
                "percentage": score / score_total,
                "score": round(score, 3),
                "description": category["description"],
                "metrics": metrics,
                "available_metrics": available_metrics[:5],
            }
        )
    return _normalize_phase_distribution_items(items)


def _phase_taxonomy_distribution_sets(
    player_card: Dict[str, Any],
    metric_docs: List[Dict[str, Any]],
    phase: str,
) -> List[Dict[str, Any]]:
    selected_roles = _phase_taxonomy_roles(player_card or {})
    sets: List[Dict[str, Any]] = []
    seen_taxonomy_keys: set[str] = set()
    for role, _role_weight in selected_roles:
        taxonomy_key = _taxonomy_key_for_role(role)
        if not taxonomy_key or taxonomy_key in seen_taxonomy_keys:
            continue
        distribution = _phase_taxonomy_distribution_for_role(role, metric_docs, phase)
        if distribution:
            sets.append({"role": role, "items": distribution})
            seen_taxonomy_keys.add(taxonomy_key)
    return sets


def _phase_taxonomy_distribution(
    player_card: Dict[str, Any],
    metric_docs: List[Dict[str, Any]],
    phase: str,
) -> List[Dict[str, Any]]:
    sets = _phase_taxonomy_distribution_sets(player_card, metric_docs, phase)
    merged: List[Dict[str, Any]] = []
    selected_roles = _phase_taxonomy_roles(player_card or {})
    role_weights = {role: weight for role, weight in selected_roles}
    for distribution_set in sets:
        role = str(distribution_set.get("role") or "")
        role_weight = float(role_weights.get(role) or 100.0)
        for item in distribution_set.get("items") or []:
            merged.append({**item, "percentage": float(item.get("percentage") or 0) * role_weight / 100.0})
    return _normalize_phase_distribution_items(merged)


def _normalized_position_names(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    names: List[str] = []
    for raw_role in value:
        short = _role_short(raw_role)
        if short and short not in names:
            names.append(short)
    return names


def _role_constraint_block(player_card: Dict[str, Any]) -> str:
    raw_roles: List[Any] = []
    position_counts = _normalized_position_counts(
        player_card.get("position_counts") or player_card.get("positionCounts")
    )
    raw_roles.extend(position_counts.keys())

    position_names = _normalized_position_names(
        player_card.get("position_names_seen") or player_card.get("positionNamesSeen")
    )
    raw_roles.extend(position_names)

    raw_roles.extend(
        value for value in (
            player_card.get("primary_position_code"),
            player_card.get("primaryPositionCode"),
        )
        if value
    )

    roles = player_card.get("roles")
    if isinstance(roles, list):
        raw_roles.extend(roles)
    elif roles:
        raw_roles.append(roles)
    raw_roles.extend(
        value for value in (
            player_card.get("position_name"),
            player_card.get("position"),
            player_card.get("role"),
        )
        if value
    )

    mapped = []
    for role in raw_roles:
        short = _role_short(role)
        if short and short not in mapped:
            mapped.append(short)

    if not mapped:
        return (
            "ROLE_CONSTRAINTS:\n"
            "- No reliable role was provided. Do not invent a new position; keep role recommendations generic and avoid naming a different position.\n"
        )

    primary = mapped[0]
    allowed_parts: List[str] = []
    forbidden_parts: List[str] = []
    for role in mapped:
        constraint = ROLE_USAGE_CONSTRAINTS.get(role, {})
        allowed = constraint.get("allowed", ROLE_SHORT_TO_LONG.get(role, role))
        forbidden = constraint.get("forbidden")
        if allowed and allowed not in allowed_parts:
            allowed_parts.append(allowed)
        if forbidden and forbidden not in forbidden_parts:
            forbidden_parts.append(forbidden)
    mapped_labels = ", ".join(f"{short} ({ROLE_SHORT_TO_LONG.get(short, short)})" for short in mapped)
    counts_label = ", ".join(f"{role}: {count}" for role, count in position_counts.items())

    lines = [
        "ROLE_CONSTRAINTS:",
        f"- Source roles mapped from the player data: {mapped_labels}.",
    ]
    if counts_label:
        lines.append(f"- Observed role distribution from position_counts, ordered by usage: {counts_label}.")
    lines.extend(
        [
            f"- Primary role for Role & Usage recommendations: {primary} ({ROLE_SHORT_TO_LONG.get(primary, primary)}), selected from the most frequent observed role when position_counts is available.",
            f"- Allowed recommendation space: {'; '.join(allowed_parts) or ROLE_SHORT_TO_LONG.get(primary, primary)}.",
            f"- Forbidden recommendation space: {'; '.join(forbidden_parts) or 'any unrelated role family'}.",
            "- In CONCLUSION / Role & Usage, every role, system, in-possession, and out-of-possession recommendation MUST stay inside the observed role set when position_counts exists.",
            "- If multiple observed roles exist, interpret the player as a multi-role profile weighted by the role distribution, with the most frequent role as the main reference.",
            "- If metrics suggest a different role family, ignore that temptation and explain how those metrics help the mapped observed role set instead.",
        ]
    )
    return "\n".join(lines)


def _metric_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


NEGATIVE_METRIC_BY_KEY = {_metric_key(metric): metric for metric in NEGATIVE_METRIC_RANGES}


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else None
    raw = str(value).strip().replace("%", "").replace(",", ".")
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    return number if number == number else None


def _iter_negative_metric_values(metadata: Dict[str, Any]) -> List[Tuple[str, float]]:
    values: Dict[str, float] = {}
    meta = metadata or {}

    for raw_metric, raw_value in meta.items():
        metric = NEGATIVE_METRIC_BY_KEY.get(_metric_key(raw_metric))
        if not metric:
            continue
        value = _num(raw_value)
        if value is not None:
            values[metric] = value

    for container_key in ("stats", "statistics", "metrics"):
        raw_stats = meta.get(container_key)
        if not isinstance(raw_stats, list):
            continue
        for stat in raw_stats:
            if not isinstance(stat, dict):
                continue
            metric = NEGATIVE_METRIC_BY_KEY.get(
                _metric_key(stat.get("metric") or stat.get("stat") or stat.get("label") or stat.get("name"))
            )
            if not metric:
                continue
            value = _num(stat.get("value") or stat.get("amount") or stat.get("score"))
            if value is not None:
                values[metric] = value

    return sorted(values.items())


def _build_metric_significance_block(metric_docs: List[Dict[str, Any]]) -> str:
    strongest_values: Dict[str, float] = {}
    for doc in metric_docs or []:
        for metric, value in _iter_negative_metric_values(doc.get("metadata") or {}):
            previous = strongest_values.get(metric)
            if previous is None or value > previous:
                strongest_values[metric] = value

    if not strongest_values:
        return "\nMETRIC_SIGNIFICANCE_GUIDE:\nNo normalized risk metrics available."

    concern_lines: List[str] = []
    low_risk_lines: List[str] = []

    for metric, value in sorted(strongest_values.items()):
        min_value, max_value = NEGATIVE_METRIC_RANGES[metric]
        if max_value <= min_value:
            continue
        risk = max(0.0, min(1.0, (value - min_value) / (max_value - min_value)))
        line = f"- {metric}: value={value:g}, risk={risk:.2f}"
        if risk >= CONCERN_RISK_THRESHOLD:
            severity = "problem" if risk >= WATCH_RISK_THRESHOLD else "watch"
            concern_lines.append(f"{line}, concern_level={severity}")
        else:
            low_risk_lines.append(f"{line}, concern_level=low")

    lines = [
        "\nMETRIC_SIGNIFICANCE_GUIDE:",
        "For negative/risk metrics, risk is normalized as (value - min) / (max - min).",
        "Only use CONCERN_CANDIDATES as direct weaknesses. Do not cite LOW_RISK_NEGATIVES as weaknesses.",
        "If a low-risk negative metric is mentioned in PLAYER STATS, keep it factual or positive-neutral; do not frame it as a concern.",
        "CONCERN_CANDIDATES:",
    ]
    lines.extend(concern_lines or ["- None"])
    lines.append("LOW_RISK_NEGATIVES:")
    lines.extend(low_risk_lines or ["- None"])
    return "\n".join(lines)


def _metric_value_from_metadata(metadata: Dict[str, Any], metric_name: str) -> Optional[Any]:
    if not isinstance(metadata, dict):
        return None

    target_key = _metric_key(metric_name)
    for raw_metric, raw_value in metadata.items():
        if _metric_key(raw_metric) == target_key:
            return raw_value

    for container_key in ("stats", "statistics", "metrics"):
        raw_stats = metadata.get(container_key)
        if not isinstance(raw_stats, list):
            continue
        for stat in raw_stats:
            if not isinstance(stat, dict):
                continue
            raw_name = stat.get("metric") or stat.get("stat") or stat.get("label") or stat.get("name")
            if _metric_key(raw_name) != target_key:
                continue
            return stat.get("value") or stat.get("amount") or stat.get("score")

    return None


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _derived_metric_value(metric_name: str, metric_docs: List[Dict[str, Any]]) -> Optional[float]:
    dependencies = {
        "Shot Quality (%)": ("Expected Goals", "Shots Total"),
        "On-Target Shot Quality (%)": ("Expected Goals On Target", "Shots On Target"),
        "Goal Conversion (%)": ("Goals", "Shots Total"),
        "On-Target to Goal Conversion (%)": ("Goals", "Shots On Target"),
        "Assist Efficiency (%)": ("Assists", "Key Passes"),
        "Dribble Accuracy (%)": ("Successful Dribbles", "Dribble Attempts"),
    }
    if metric_name not in dependencies:
        return None

    numerator_metric, denominator_metric = dependencies[metric_name]
    numerator: Optional[float] = None
    denominator: Optional[float] = None
    for doc in metric_docs or []:
        metadata = doc.get("metadata") or {}
        if numerator is None:
            numerator = _to_float(_metric_value_from_metadata(metadata, numerator_metric))
        if denominator is None:
            denominator = _to_float(_metric_value_from_metadata(metadata, denominator_metric))
        if numerator is not None and denominator is not None:
            break

    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


DERIVED_EFFICIENCY_METRICS: Dict[str, str] = {
    "Shot Quality (%)": "chance quality per shot attempt",
    "On-Target Shot Quality (%)": "danger level of shots that hit the target",
    "Goal Conversion (%)": "finishing conversion from all shot attempts",
    "On-Target to Goal Conversion (%)": "finishing conversion from shots on target",
    "Assist Efficiency (%)": "assist return from key-pass volume",
    "Dribble Accuracy (%)": "take-on reliability from dribble attempts",
}


def _build_derived_efficiency_context(metric_docs: List[Dict[str, Any]]) -> str:
    values: List[str] = []
    for metric, meaning in DERIVED_EFFICIENCY_METRICS.items():
        selected = _derived_metric_value(metric, metric_docs)
        if selected is None:
            for doc in metric_docs or []:
                selected = _to_float(_metric_value_from_metadata(doc.get("metadata") or {}, metric))
                if selected is not None:
                    break
        if selected is not None:
            values.append(f"- {metric}: value={selected:g}, meaning={meaning}")

    lines = [
        "\nDERIVED_EFFICIENCY_CONTEXT:",
        "Use these derived percentage metrics as interpretation signals wherever tactically relevant, including PLAYER STATS, ABSTRACT, Role & Usage, Strengths, Weaknesses & Concerns, Match Phases, and category perspectives.",
        "Do not force every metric into every section. Use the signal only when it sharpens the scouting point.",
        "Treat these as percentage metrics where higher is generally better. Avoid raw formula explanations in the report text.",
        "Available derived efficiency metrics:",
    ]
    lines.extend(values or ["- None"])
    return "\n".join(lines)


def _build_category_metric_context(player_card: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> str:
    category_values: Dict[str, List[str]] = {}
    position_counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    if position_counts:
        total = sum(position_counts.values())
        values = []
        for role, count in position_counts.items():
            percent = round((count / total) * 100) if total else 0
            values.append(f"{role}={count} appearances / {percent}%")
        category_values["Pitch Map"] = values

    for category, metrics in CATEGORY_PERSPECTIVE_METRICS.items():
        values: List[str] = []
        for metric in metrics:
            selected: Optional[Any] = _derived_metric_value(metric, metric_docs)
            for doc in metric_docs or []:
                if selected not in (None, ""):
                    break
                selected = _metric_value_from_metadata(doc.get("metadata") or {}, metric)
                if selected not in (None, ""):
                    break
            if selected not in (None, ""):
                values.append(f"{metric}={selected}")
        if values:
            category_values[category] = values

    lines = [
        "\nCATEGORY_METRIC_CONTEXT:",
        "Use this block to write CATEGORY PERSPECTIVES. Each listed category maps to one report metric page in the UI.",
        f"REQUIRED_CATEGORY_PERSPECTIVES: {', '.join(category_values.keys()) if category_values else 'None'}.",
        "You must output exactly one CATEGORY PERSPECTIVES bullet for every category in REQUIRED_CATEGORY_PERSPECTIVES. Missing any required category is invalid.",
        "Do not repeat the raw metric names or values in the perspective text; use them only to reason.",
        "Write a deeper scouting interpretation: first frame the general profile, then add one sharp, confident takeaway.",
        "For Pitch Map, interpret the player's observed zones and role relationships. Do not mention raw role counts, percentages, or phrases such as all matches / 100%. Use the distribution only as background reasoning.",
        "For Pitch Map, if multiple connected positions exist, explain the player's ability to move between related zones; if the profile is role-specialized, describe the tactical meaning without quoting the percentage.",
        "For Errors & Discipline, lower values are generally better. Interpret it as a risk-control / discipline profile, not as a positive-volume category.",
        "Use the player's name naturally when it helps the sentence. Name usage is allowed in every category, including Defending and Errors & Discipline.",
        "Available category metrics:",
    ]
    if not category_values:
        lines.append("- None")
    else:
        for category, values in category_values.items():
            lines.append(f"- {category}: {', '.join(values)}")
    return "\n".join(lines)


def _build_phase_fit_context(player_card: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> str:
    position_counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    is_goalkeeper = _is_goalkeeper_card(player_card or {})
    top_roles = _top_position_roles(player_card or {}, 2)
    phase_metrics = GK_PHASE_FIT_METRICS if is_goalkeeper else PHASE_FIT_METRICS
    required_phase_names = _required_phase_names(player_card or {})
    required_phases = ", ".join(required_phase_names)
    omitted_phases = [phase for phase in OUTFIELD_PHASES if phase not in required_phase_names]
    primary_role = top_roles[0] if top_roles else None
    role_family = _phase_role_family(player_card or {})
    taxonomy_roles = _phase_taxonomy_roles(player_card or {})
    taxonomy_role_text = ", ".join(f"{role}={round(weight)}%" for role, weight in taxonomy_roles) if taxonomy_roles else "None"
    role_distribution = "None"
    if position_counts:
        total = sum(position_counts.values())
        role_distribution = ", ".join(
            f"{role}={count} appearances / {round((count / total) * 100) if total else 0}%"
            for role, count in position_counts.items()
        )
    lines = [
        "\nPHASE_FIT_CONTEXT:",
        f"Required PHASE FIT bullets: {required_phases}.",
        f"Omitted PHASE FIT bullets: {', '.join(omitted_phases) if omitted_phases else 'None'}.",
        f"Phase role family: {role_family}.",
        f"Top two observed roles for phase logic: {', '.join(top_roles) if top_roles else 'None'}.",
        f"Primary observed role for phase logic: {primary_role or 'None'}.",
        f"Selected taxonomy roles after 20-point rule: {taxonomy_role_text}.",
        "Taxonomy role selection rule: if the first observed role is more than 20 percentage points ahead of the second role, use only the first role; otherwise use the first two roles and normalize their weights to 100.",
        "NEW PHASE FIT OUTPUT RULE: each phase must contain exactly two pipe-separated parts: (1) category percentage distribution, and (2) one ScoutWise perspective sentence.",
        "If PHASE_TAXONOMY_DISTRIBUTION contains multiple ROLE VIEW blocks for a phase, the distribution part must keep those role views separate using this exact format: 'ROLE: Category Name 42%, Category Name 58% || ROLE: Category Name 35%, Category Name 65%'.",
        "Category names in the distribution are strict machine labels: copy them exactly from PHASE_TAXONOMY_DISTRIBUTION. Do not translate, shorten, paraphrase, rename, or localize them even when the report language is Turkish; the UI translates these labels.",
        "The ROLE prefix is mandatory for every role view when multiple ROLE VIEW blocks exist. Never output only 'Category 42% || Category 58%' without the role labels.",
        "If PHASE_TAXONOMY_DISTRIBUTION contains one ROLE VIEW block for a phase, write only 'Category Name 42%, Category Name 58%' without a role prefix.",
        "Each role view's percentages must sum to 100 by itself. Do not merge role views into one combined distribution.",
        "The category percentages are metric-weighted using the available signals for each taxonomy category; do not equalize them unless the metric evidence is genuinely balanced.",
        "Write distribution entries as 'Category Name 42%' separated by commas. Do not use '=' signs.",
        "The perspective sentence must be one strong sentence using the distribution and available metric signals; do not add extra bullets or extra pipe parts.",
        f"CF in top two observed roles: {'yes' if 'CF' in top_roles else 'no'}.",
        f"CF is primary observed role: {'yes' if primary_role == 'CF' else 'no'}.",
        "If this player is a goalkeeper, write ONLY Build-up and Low Block; do not write Progression, Final Third, Mid Block, or High Block.",
        "For a goalkeeper, use only the GK taxonomy categories in PHASE_TAXONOMY_DISTRIBUTION.",
        "For a goalkeeper, Build-up means the scenario where the goalkeeper's team has the ball and uses the goalkeeper in first-phase possession, distribution, security, and pressure release.",
        "For a goalkeeper, Low Block means the scenario where the opponent has the ball and the goalkeeper protects the goal, box, aerial space, and last defensive line.",
        "Use taxonomy categories and available metric signals as reasoning evidence; mention only one or two metric values when they sharpen the sentence.",
        f"Observed role distribution to consider in every phase: {role_distribution}.",
        "Phase interpretation must connect the selected taxonomy distribution with the player's statistical style and likely tactical behavior in that phase.",
        "If a phase has sparse metrics, infer cautiously from taxonomy role distribution, role constraints, team role, and available adjacent phase metrics. Do not mention missing data.",
        "The UI will show each phase with a vertical pitch and highlighted zone, so each explanation should focus on how the player fits that phase tactically.",
        "PHASE_TAXONOMY_DISTRIBUTION:",
    ]
    for phase in required_phase_names:
        distribution_sets = _phase_taxonomy_distribution_sets(player_card or {}, metric_docs, phase)
        if not distribution_sets:
            lines.append(f"- {phase}: No taxonomy distribution available; use role/context only.")
            continue
        lines.append(f"- {phase}:")
        for distribution_set in distribution_sets:
            role = distribution_set.get("role")
            lines.append(f"  - ROLE VIEW {role}:")
            for item in distribution_set.get("items") or []:
                signals = "; available signals: " + ", ".join(item["available_metrics"]) if item["available_metrics"] else ""
                metrics = "; category metrics: " + ", ".join(item["metrics"][:8]) if item.get("metrics") else ""
                lines.append(
                    f"    - {item['category']} {item['percentage']}% "
                    f"(from {item['source_role']}; {item['description']}{signals}{metrics})"
                )
    return "\n".join(lines)


def fetch_docs_for_favorite(
    db,
    player_identity: Dict[str, Any],
    limit_docs: int = 30,
) -> List[Dict[str, Any]]:
    club_player_id = player_identity.get("club_player_id") or player_identity.get("clubPlayerId")
    if club_player_id is not None:
        row = db.execute(
            text(
                """
                SELECT id, metadata, content
                FROM player_data
                WHERE id = :player_id
                LIMIT 1
                """
            ),
            {"player_id": club_player_id},
        ).mappings().first()
        if row:
            return [{"id": row["id"], "content": row.get("content"), "metadata": row.get("metadata")}]

    name = player_identity.get("name")
    if not name or not str(name).strip():
        return []

    name_raw = str(name).strip()
    name_norm = norm_name(name_raw)
    name_raw_q = f"%{name_raw}%"
    name_norm_q = f"%{name_norm}%"

    nat = player_identity.get("nationality")
    nat_raw = nat.strip() if isinstance(nat, str) else ""
    nat_q = f"%{nat_raw}%" if nat_raw else None

    rows = db.execute(
        text(
            """
            SELECT id, metadata, content
            FROM player_data
            WHERE
            (
                (metadata->>'player_name_norm') ILIKE :name_norm_q
                OR (metadata->>'player_name') ILIKE :name_raw_q
                OR (content ILIKE :name_raw_q)
            )
            AND (
                :nat_q IS NULL
                OR (metadata->>'nationality_name') ILIKE :nat_q
                OR (content ILIKE :nat_q)
            )
            ORDER BY id DESC
            LIMIT :lim
            """
        ),
        {
            "name_norm_q": name_norm_q,
            "name_raw_q": name_raw_q,
            "nat_q": nat_q,
            "lim": int(limit_docs),
        },
    ).mappings().all()

    if not rows:
        rows = db.execute(
            text(
                """
                SELECT id, metadata, content
                FROM player_data
                WHERE
                (
                    (metadata->>'player_name_norm') ILIKE :name_norm_q
                    OR (metadata->>'player_name') ILIKE :name_raw_q
                    OR (content ILIKE :name_raw_q)
                )
                ORDER BY id DESC
                LIMIT :lim
                """
            ),
            {"name_norm_q": name_norm_q, "name_raw_q": name_raw_q, "lim": int(limit_docs)},
        ).mappings().all()
    if not rows:
        return []

    best: Tuple[float, Optional[int]] = (-1.0, None)
    for row in rows:
        score = _score_candidate(row.get("metadata") or {}, player_identity)
        row_id = row.get("id")
        if row_id is not None and score > best[0]:
            best = (score, int(row_id))

    if best[1] is None:
        return [{"id": row["id"], "content": row.get("content"), "metadata": row.get("metadata")} for row in rows[:limit_docs]]

    doc = db.execute(
        text(
            """
            SELECT id, metadata, content
            FROM player_data
            WHERE id = :id
            LIMIT 1
            """
        ),
        {"id": best[1]},
    ).mappings().first()
    if not doc:
        return []

    return [{"id": doc["id"], "content": doc.get("content"), "metadata": doc.get("metadata")}]


def build_player_card_from_docs(metric_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    card: Dict[str, Any] = {}

    for doc in metric_docs:
        meta = doc.get("metadata") or {}

        fields = {
            "name": _first_non_empty(meta.get("player_name"), meta.get("name"), meta.get("player")),
            "team": _first_non_empty(meta.get("team"), meta.get("team_name"), meta.get("club")),
            "league": _first_non_empty(meta.get("league"), meta.get("league_name")),
            "nationality": _first_non_empty(meta.get("nationality"), meta.get("nationality_name"), meta.get("country")),
            "gender": _first_non_empty(meta.get("gender")),
            "age": _first_non_empty(meta.get("age")),
            "height": _first_non_empty(meta.get("height"), meta.get("height_cm")),
            "weight": _first_non_empty(meta.get("weight"), meta.get("weight_kg")),
            "potential": _first_non_empty(meta.get("potential")),
            "form": _first_non_empty(meta.get("form")),
            "position_name": _first_non_empty(meta.get("position_name"), meta.get("position")),
        }
        for key, value in fields.items():
            if key not in card and value is not None:
                card[key] = value

        position_counts = _normalized_position_counts(meta.get("position_counts"))
        if position_counts and "position_counts" not in card:
            card["position_counts"] = position_counts

        position_names_seen = _normalized_position_names(meta.get("position_names_seen"))
        if position_names_seen and "position_names_seen" not in card:
            card["position_names_seen"] = position_names_seen

        if "position_count_total" not in card:
            total = _first_non_empty(meta.get("position_count_total"))
            if total is None and position_counts:
                total = sum(position_counts.values())
            if total is not None:
                card["position_count_total"] = total

        if "primary_position_code" not in card:
            primary = _role_short(_first_non_empty(meta.get("primary_position_code")))
            if not primary and position_counts:
                primary = next(iter(position_counts.keys()), None)
            if primary:
                card["primary_position_code"] = primary

        if "roles" not in card:
            if card.get("position_counts"):
                card["roles"] = list(card["position_counts"].keys())
            elif card.get("position_names_seen"):
                card["roles"] = card["position_names_seen"]
            elif card.get("position_name"):
                card["roles"] = [str(card["position_name"])]
            else:
                roles_raw = _first_non_empty(meta.get("roles"), meta.get("roles_json"), meta.get("position"), meta.get("position_name"))
                card["roles"] = _normalize_roles(roles_raw)

    if "roles" not in card:
        card["roles"] = [str(card["position_name"])] if card.get("position_name") else []

    return card


def _build_llm_input(player_card: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> str:
    parts: List[str] = ["PLAYER_CARD_JSON:", str(player_card or {}), "\nMETRIC_DOCUMENTS (newest first):"]
    parts.insert(0, _role_constraint_block(player_card))
    parts.insert(1, _build_metric_significance_block(metric_docs))
    parts.insert(2, _build_derived_efficiency_context(metric_docs))
    parts.insert(3, _build_category_metric_context(player_card, metric_docs))
    parts.insert(4, _build_phase_fit_context(player_card, metric_docs))

    if not metric_docs:
        parts.append("[]")
    else:
        for doc in metric_docs[:30]:
            meta = doc.get("metadata") or {}
            content = (doc.get("content") or "").strip()
            if len(content) > 1200:
                content = content[:1200] + "..."
            parts.append(f"\n- doc_id: {doc.get('id')}")
            parts.append(f"  metadata: {meta}")
            parts.append(f"  content: {content}")

    return "\n".join(parts)


def generate_report_content(
    db,
    favorite_id: str,
    lang: str = "en",
    version: int = 1,
    player_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    identity = player_identity or {}
    docs = fetch_docs_for_favorite(db, player_identity=identity, limit_docs=30)
    player_card = build_player_card_from_docs(docs)

    for key, value in identity.items():
        if key not in player_card and value is not None:
            player_card[key] = value
    if identity.get("roles"):
        player_card["roles"] = identity["roles"]
    identity_counts = _normalized_position_counts(identity.get("position_counts") or identity.get("positionCounts"))
    if identity_counts:
        player_card["position_counts"] = identity_counts
        player_card["roles"] = list(identity_counts.keys())
        if not player_card.get("position_count_total"):
            player_card["position_count_total"] = sum(identity_counts.values())
        player_card["primary_position_code"] = next(iter(identity_counts.keys()), None)
    identity_names = _normalized_position_names(identity.get("position_names_seen") or identity.get("positionNamesSeen"))
    if identity_names and not player_card.get("position_names_seen"):
        player_card["position_names_seen"] = identity_names
    if identity.get("position_count_total") or identity.get("positionCountTotal"):
        player_card["position_count_total"] = identity.get("position_count_total") or identity.get("positionCountTotal")
    if identity.get("primary_position_code") or identity.get("primaryPositionCode"):
        player_card["primary_position_code"] = identity.get("primary_position_code") or identity.get("primaryPositionCode")
    for role_key in ("position_name", "position", "role"):
        if identity.get(role_key):
            player_card[role_key] = identity[role_key]
    for score_key in ("potential", "form"):
        if player_card.get(score_key) in (None, "") and identity.get(score_key) is not None:
            player_card[score_key] = identity[score_key]

    report_text = (report_chain.invoke({"input_text": _build_llm_input(player_card, docs), "lang": lang}) or "").strip()
    content_json = {
        "favorite_player_id": favorite_id,
        "language": lang,
        "version": version,
        "player_identity": identity,
        "player_card": player_card,
        "metrics_docs": docs,
        "report_text": report_text,
    }
    return {"content": report_text, "content_json": content_json}
