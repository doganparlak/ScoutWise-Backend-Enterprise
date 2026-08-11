from __future__ import annotations

import unittest
from unittest.mock import patch

from player_comp_season_module.season_data import (
    _search_norm,
    aggregate_player_seasons,
)


def season_row(
    *,
    team_id: int,
    league_id: int,
    season_id: int,
    matches: int,
    goals: float,
    positions: dict[str, int],
) -> dict:
    return {
        "player_id": 10,
        "player_name": "Barış Alper Yılmaz",
        "team_id": team_id,
        "team_name": "Galatasaray",
        "league_id": league_id,
        "league_name": "Competition",
        "league_type": "league",
        "league_sub_type": "domestic",
        "league_country_name": "Türkiye",
        "league_short_code": "TEST",
        "league_image_path": "",
        "season_id": season_id,
        "season_name": "2025/2026",
        "gender": "male",
        "height": 186,
        "weight": 80,
        "age": 26,
        "match_count": matches,
        "nationality_name": "Türkiye",
        "position_name": "Left Midfield",
        "position_counts": positions,
        "stats": {"goals": goals, "rating": 7.0, "ignored": None},
        "updated_at": season_id,
    }


class PlayerCompSeasonTests(unittest.TestCase):
    def test_search_normalization_handles_turkish_accents_and_punctuation(self) -> None:
        self.assertEqual(_search_norm("  BARİŞ  Alper-Yılmaz "), "baris alper yilmaz")
        self.assertEqual(_search_norm("B. Yılmaz"), "b yilmaz")

    @patch("player_comp_season_module.season_data._fetch_player_rows")
    def test_single_row_returns_that_rows_metrics(self, fetch_rows) -> None:
        row = season_row(
            team_id=34,
            league_id=2,
            season_id=25580,
            matches=12,
            goals=0.25,
            positions={"LM": 8, "CF": 4},
        )
        fetch_rows.return_value = [row]
        result = aggregate_player_seasons(
            object(),
            10,
            [{"teamId": 34, "leagueId": 2, "seasonId": 25580}],
        )
        self.assertEqual(result["matchCount"], 12)
        self.assertEqual(result["stats"]["goals"], 0.25)
        self.assertEqual(result["positionCounts"], {"LM": 8.0, "CF": 4.0})

    @patch("player_comp_season_module.season_data._fetch_player_rows")
    def test_multiple_rows_are_rejected(self, fetch_rows) -> None:
        first = season_row(
            team_id=34,
            league_id=2,
            season_id=25580,
            matches=12,
            goals=0.25,
            positions={"LM": 8, "CF": 4},
        )
        second = season_row(
            team_id=34,
            league_id=600,
            season_id=25682,
            matches=30,
            goals=0.30,
            positions={"LM": 17, "CF": 8, "RM": 5},
        )
        fetch_rows.return_value = [second, first]
        with self.assertRaisesRegex(ValueError, "Only one season row"):
            aggregate_player_seasons(
                object(),
                10,
                [
                    {"teamId": 34, "leagueId": 2, "seasonId": 25580},
                    {"teamId": 34, "leagueId": 600, "seasonId": 25682},
                ],
            )

    @patch("player_comp_season_module.season_data._fetch_player_rows")
    def test_zero_values_are_not_reinterpreted_as_missing(self, fetch_rows) -> None:
        unavailable = season_row(
            team_id=34,
            league_id=2,
            season_id=25580,
            matches=30,
            goals=0.0,
            positions={"AM": 30},
        )
        unavailable["stats"]["rating"] = 0.0
        available = season_row(
            team_id=34,
            league_id=600,
            season_id=25682,
            matches=6,
            goals=0.5,
            positions={"AM": 6},
        )
        available["stats"]["rating"] = 7.5
        fetch_rows.return_value = [available, unavailable]

        result = aggregate_player_seasons(
            object(),
            10,
            [{"teamId": 34, "leagueId": 2, "seasonId": 25580}],
        )

        self.assertEqual(result["matchCount"], 30)
        self.assertEqual(result["stats"]["goals"], 0.0)
        self.assertEqual(result["stats"]["rating"], 0.0)

    @patch("player_comp_season_module.season_data._fetch_player_rows")
    def test_rejects_duplicate_or_foreign_sources(self, fetch_rows) -> None:
        row = season_row(
            team_id=34,
            league_id=2,
            season_id=25580,
            matches=12,
            goals=0.25,
            positions={"LM": 12},
        )
        fetch_rows.return_value = [row]
        source = {"teamId": 34, "leagueId": 2, "seasonId": 25580}
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            aggregate_player_seasons(object(), 10, [source, source])
        with self.assertRaisesRegex(ValueError, "not found"):
            aggregate_player_seasons(
                object(),
                10,
                [{"teamId": 99, "leagueId": 2, "seasonId": 25580}],
            )


if __name__ == "__main__":
    unittest.main()
