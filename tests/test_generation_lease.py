import unittest
from unittest.mock import MagicMock,patch
from report_module import generation_lease as lease

class MatchReportLeaseTests(unittest.TestCase):
    def test_claim_is_scoped_to_the_account_and_report(self):
        engine=MagicMock();engine.begin.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value=True
        with patch.object(lease,'engine',engine):
            with lease.match_report_lease('report-a','account-a')as acquired:self.assertTrue(acquired)
        params=engine.begin.return_value.__enter__.return_value.execute.call_args.args[1]
        self.assertEqual(params['key'],'enterprise-match-report:account-a:report-a')
        engine.begin.return_value.__exit__.assert_called_once()

    def test_live_owner_is_not_claimed_twice(self):
        engine=MagicMock();engine.begin.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value=False
        with patch.object(lease,'engine',engine):
            with lease.match_report_lease('report-a','account-a')as acquired:self.assertFalse(acquired)

    def test_worker_error_releases_transaction(self):
        engine=MagicMock();engine.begin.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value=True
        with patch.object(lease,'engine',engine):
            with self.assertRaises(RuntimeError):
                with lease.match_report_lease('report-a','account-a'):raise RuntimeError('interrupted')
        self.assertEqual(engine.begin.return_value.__exit__.call_args.args[0],RuntimeError)
