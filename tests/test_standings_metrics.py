import unittest
from datetime import datetime, timezone

from standings_module.metrics import Aggregate, period_for


def player(pid, team, rating, minutes=90):
    return {'playerId': pid, 'teamId': team, 'name': f'Player {pid}',
            'imageUrl': None, 'rating': rating, 'minutes': minutes}


def match(day, players, included=True):
    return {'kickoff_at': datetime(2026, 1, day, tzinfo=timezone.utc),
            'included_in_aggregation': included,
            'team_contributions': {
                '1': {'name': 'Home', 'metrics': {'Shots Total': 10, 'Ball Possession %': 60}},
                '2': {'name': 'Away', 'metrics': {'Shots Total': 5, 'Ball Possession %': 40}},
            }, 'player_contributions': players}


class PlayerOnlyAggregationTests(unittest.TestCase):
    def assert_same_player_results(self, rows):
        full, players_only = Aggregate(), Aggregate(include_team_metrics=False)
        for row in rows:
            full.add(row)
            players_only.add(row)
        expected, actual = full.finish(), players_only.finish()
        for field in ('player_rating_totals', 'team_best_players', 'league_best_player',
                      'fixture_count', 'latest_included_kickoff_at', 'coverage'):
            self.assertEqual(expected[field], actual[field], field)
        self.assertEqual(actual['metric_catalog'], [])
        self.assertTrue(all(not team['metrics'] for team in actual['team_metrics'].values()))
        return expected, actual

    def test_ratings_ties_missing_ratings_and_transfer(self):
        rows = [
            match(5, [player(1, 1, 8, 60), player(2, 1, 8), player(3, 1, 8),
                      player(4, 2, None), player(5, 2, 7)]),
            match(10, [player(1, 1, 8, 60), player(2, 1, 8), player(5, 2, 9),
                       player(3, 2, 9), player(6, 2, 9)]),
            match(11, [player(99, 1, 10)], included=False),
        ]
        season, result = self.assert_same_player_results(rows)
        self.assertEqual(result['team_best_players']['1']['playerId'], 2)
        self.assertEqual(result['team_best_players']['2']['playerId'], 3)
        self.assertEqual(result['league_best_player']['playerId'], 3)
        self.assertEqual(result['player_rating_totals']['2:5']['ratingSum'], 16)
        self.assertEqual(result['player_rating_totals']['2:5']['ratedAppearances'], 2)
        self.assertEqual(result['player_rating_totals']['2:4']['ratedAppearances'], 0)
        self.assertEqual(result['coverage'], {'teamCount': 2, 'ratedPlayerCount': 6})
        self.assertEqual(season['team_metrics']['1']['metrics']['Shots Total']['value'], 20)
        self.assertEqual(season['team_metrics']['1']['metrics']['Ball Possession %']['value'], 60)

    def test_empty_and_unrated_periods(self):
        self.assert_same_player_results([])
        _, result = self.assert_same_player_results([match(5, [player(1, 1, None, None)])])
        self.assertEqual(result['team_best_players'], {})
        self.assertIsNone(result['league_best_player'])
        self.assertEqual(result['coverage']['teamCount'], 2)

    def test_periods_have_independent_winners(self):
        rows = [match(5, [player(1, 1, 9), player(2, 1, 7)]),
                match(19, [player(1, 1, 5), player(2, 1, 8)])]
        periods = {}
        for row in rows:
            periods.setdefault(period_for(row['kickoff_at'].date())[0], []).append(row)
        winners = [self.assert_same_player_results(group)[1]['league_best_player']['playerId']
                   for group in periods.values()]
        self.assertEqual(winners, [1, 2])
        self.assertEqual(self.assert_same_player_results(rows)[1]['league_best_player']['playerId'], 2)

    def test_player_only_mode_does_not_read_team_statistics(self):
        row = match(5, [player(1, 1, 8)])
        for team in row['team_contributions'].values():
            del team['metrics']
        aggregate = Aggregate(include_team_metrics=False)
        aggregate.add(row)
        self.assertEqual(aggregate.finish()['league_best_player']['averageRating'], 8)


if __name__ == '__main__':
    unittest.main()
