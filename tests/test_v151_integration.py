"""Integration edge cases found during v1.5.1 acceptance (offline HTTP)."""
import csv
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import test_movie_history as f
from zgdypf import db, national_history as n, history_roster as r

class IntegrationTests(unittest.TestCase):
    setUp = f.MovieHistoryTests.setUp
    client = f.MovieHistoryTests.client
    fake_open = f.MovieHistoryTests.fake_open

    def seed(self, days=(f.DAY,), codes=f.CODES):
        with self.conn:
            for day in days:
                r._merge(self.conn, day, dict.fromkeys(codes), "csv:acceptance")
                r._save_refresh(self.conn, day, "seed", "listed", len(codes), 1, len(codes))

    def run_batch(self, end=f.DAY, **opts):
        plan = n.make_plan(self.path, f.MOVIE, f.DAY, end, **opts)
        return n.run_plan(plan, self.root/'raw', self.root/'out', client_factory=self.client,
                          full_matrix=False, emit=lambda _: None)

    def test_failed_first_cinema_does_not_starve_remaining_204s(self):
        self.seed(codes=f.CODES[:3])
        self.routes = [(204, None)] * 3
        self.run_batch(retry_empty=True)
        self.calls.clear()
        self.routes = [(503, {})] * 2 + [(204, None)] * 2
        for _ in range(3):
            self.run_batch(max_requests=1, retry_empty=True)
        codes = [parse_qs(urlsplit(u).query)['cinemaCode'][0] for u in self.calls]
        self.assertEqual(len(set(codes)), 3, codes)

    def test_mixed_success_day_yields_next_batch_to_other_date(self):
        self.seed(days=(f.DAY, '2026-05-01'))
        self.routes = [(200, f.box()), (204, None)]
        for _ in range(2):
            self.run_batch(end='2026-05-01', max_requests=1, retry_empty=True)
        self.assertIn('2026-05-01', self.calls[1])

    def test_changed_rotation_invalidates_reviewed_plan(self):
        self.seed(codes=f.CODES[:1])
        self.routes = [(204, None)]
        self.run_batch(retry_empty=True)
        old = n.make_plan(self.path, f.MOVIE, f.DAY, f.DAY, retry_empty=True)
        self.routes = [(204, None)]
        self.run_batch(retry_empty=True)
        with self.assertRaisesRegex(ValueError, '计划'):
            n.run_plan(old, self.root/'raw', self.root/'out', client_factory=self.client)

    def test_204_ledger_and_rotation_rollback_together(self):
        from zgdypf.cinema_detail import collect
        self.routes = [(204, None)]
        self.conn.execute("""CREATE TRIGGER reject_rotation BEFORE INSERT ON history_204_review_queue
                             BEGIN SELECT RAISE(ABORT,'rotation failure'); END""")
        with self.assertRaisesRegex(Exception, 'rotation failure'):
            collect(self.conn, self.client(), f.CODES[0], f.DAY, record_attempt=True)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collect_ledger').fetchone()[0], 0)

    def test_light_audit_preserves_existing_matrix_and_exact_money(self):
        self.seed(codes=f.CODES[:2])
        self.routes = [(200, f.box(sales=2**53+1)), (204, None)]
        self.run_batch()
        report = n.export_audit(self.path, self.root/'out', f.MOVIE, f.DAY, f.DAY)
        matrix = report['paths']['matrix']
        old = matrix.read_bytes()
        n.export_audit(self.path, self.root/'out', f.MOVIE, f.DAY, f.DAY, write_matrix=False)
        self.assertEqual(matrix.read_bytes(), old)
        with matrix.open(encoding='utf-8-sig', newline='') as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]['sales_yuan'], '90071992547409.93')
        self.assertEqual(rows[1]['sales_yuan'], '')
        self.assertFalse(report['summary']['national_complete'])

    def test_roster_review_resumes_other_thin_dates_without_repeating_first(self):
        days = (f.DAY, '2026-05-01')
        self.seed(days=days, codes=f.CODES[:1])
        with self.conn:
            self.conn.execute("UPDATE history_roster_refresh SET status='thin'")
        opts = dict(max_requests=1, minutes=1, retry_empty=True, refresh_roster=True, review_only=True)
        self.routes = [(200, {'salesRankList': [], 'hasMore': False})] * 2
        for index in range(2):
            result = self.run_batch(end=days[1], **opts)
            self.assertIn(days[index], self.calls[-1])
            opts['reviewed_roster_days'] = result['reviewed_roster_days']
        self.assertEqual(result['reviewed_roster_days'], list(days))
        self.assertEqual(len(self.calls), 2)

    def test_persistent_rotation_survives_database_close_and_reopen(self):
        self.seed(codes=f.CODES[:3])
        self.routes = [(204, None)] * 3
        for _ in range(3):
            self.run_batch(max_requests=1, retry_empty=True)
            self.conn.close()
            self.conn = db.connect(str(self.path))
            self.addCleanup(self.conn.close)
        codes = [parse_qs(urlsplit(u).query)['cinemaCode'][0] for u in self.calls]
        self.assertEqual(len(set(codes)), 3)

    def test_real_responses_replay_through_collection_and_export(self):
        # Fixtures are unmodified bounded live responses retained with provenance.
        from pathlib import Path
        live = Path(__file__).parent / 'fixtures' / 'live_20261117'
        self.seed(codes=f.CODES[:1])
        payload = json.loads((live/'cinema_box_200.json').read_bytes())
        self.routes = [(200, payload)]
        result = self.run_batch()
        self.assertEqual(result['saved'], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM fact_cinema_movie').fetchone()[0], payload['movieCount'])
        self.assertFalse(result['national_complete'])
        self.seed(days=('2017-01-01',), codes=f.CODES[:1])
        self.routes = [(204, None)]
        plan = n.make_plan(self.path, f.MOVIE, '2017-01-01', '2017-01-01', max_requests=1)
        result = n.run_plan(plan, self.root/'raw', self.root/'out', client_factory=self.client, emit=lambda _: None)
        self.assertEqual(result['empty'], 1)
        with result['reports']['paths']['matrix'].open(encoding='utf-8-sig') as fh:
            row = next(csv.DictReader(fh))
        self.assertEqual((row['status'], row['sales_fen']), ('deferred_204', ''))
