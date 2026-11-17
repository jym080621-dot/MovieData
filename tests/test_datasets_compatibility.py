"""All 24 legacy console datasets: fake HTTP, real parsers, Engine and SQLite."""
import io
import json
import unittest
from urllib.parse import urlsplit
import test_movie_history as f
from zgdypf import datasets as ds
from zgdypf.client import Client

class DatasetCompatibilityTests(unittest.TestCase):
    setUp = f.MovieHistoryTests.setUp

    def check_dataset(self, name):
        rank = {'code': f.CODES[0], 'name': '北京市测试影院', 'sales': 12503,
                'salesDesc': '125.03', 'rank': 1}
        film = {'code': f.MOVIE, 'movieCode': f.MOVIE, 'name': '示例影片',
                'showDate': f.DAY, 'sales': 12503, 'salesInWanDesc': '0.012503',
                'salesInWan': '0.012503', 'rank': 1}
        summary = {'salesDesc': {'unit': '万', 'value': '1'},
                   'splitSalesDesc': {'unit': '万', 'value': '0.9'},
                   'nationalQuotaInfo': {'movieCount': 1, 'sessionCount': 7}}
        trend = [{'dateUp': f.DAY, 'dateDown': '周四', 'sales': 12503}]
        payloads = {
            '/getDateRange': {'startDate': '2017-01-01', 'endDate': f.DAY},
            '/getNational': summary, '/getDayData': {'list': [film]},
            '/getProvinceList': {'salesRankList': [dict(rank, code='110000', name='北京')]},
            '/getChainList': {'salesRankList': [dict(rank, code='0001', name='测试院线')]},
            '/getCinemaList': {'salesRankList': [dict(rank, code=str(40000000+i)) for i in range(11010)], 'hasMore': False},
            '/getCinemaSumData': summary, '/getCinemaBoxData': f.box(), '/getCinemaManageData': trend,
            '/getChainSumData': summary, '/getChainBoxData': f.box(), '/getChainManageData': trend,
            '/getChainYesterday': {'name': '全国', 'rank': 1},
            '/getCityData': {'cityRank': [{'name': '北京', 'code': '110100', 'value': 125}]},
            '/searchCinema': [rank], '/searchMovie': [film],
            '/getPeriodData': {'list': [film]}, '/getYearTabs': {'list': [2026]},
            '/getYearData': {'rankList': [film]},
        }
        for endpoint in ('getSingleDayData', 'getReleaseDayData', 'getNationSingleDayData',
                         'getNationSingleWeekData', 'getNationSingleMonthData'):
            payloads['/'+endpoint] = {'rankList': [film], 'title': '纪录'}
        client = Client(rate=0, retries=1)
        def respond(req, timeout):
            path = urlsplit(req.full_url).path
            if path.startswith('/movie/detail/'):
                state = {'movieInfoData': {'name': '示例影片'}, 'movieIndexData': [film]}
                reply = io.BytesIO(('<script>window.__INITIAL_STATE__='+json.dumps(state)+'</script>').encode())
                reply.status = 200
                return reply
            self.assertIn(path, payloads)
            return f.Reply(200, payloads[path])
        client._open = respond
        ctx = ds.Ctx(self.conn, client, {'start': f.DAY, 'end': f.DAY}, lambda _: None)
        ctx._sample = [f.CODES[0]]
        ctx._chains = [('0001', '测试院线')]
        ctx._movies = {f.DAY: [(f.MOVIE, '示例影片')]}
        tasks = ds.build_tasks(ctx, name)
        self.assertTrue(tasks)
        stat = ds.Engine(ctx).run([(name, tasks)], lambda *args: None)
        errors = list(self.conn.execute("SELECT err FROM collect_ledger WHERE status='error'"))
        self.assertEqual(stat['err'], 0, [tuple(row) for row in errors])
        self.assertGreater(stat['ok'], 0, stat)
        self.assertGreater(client.stats.requests, 0)
        self.assertGreater(self.conn.execute('SELECT count(*) FROM collect_ledger').fetchone()[0], 0)


def dataset_test(name):
    def test(self):
        self.check_dataset(name)
    return test

for spec in ds.SPECS:
    setattr(DatasetCompatibilityTests, 'test_'+spec.id, dataset_test(spec.id))
