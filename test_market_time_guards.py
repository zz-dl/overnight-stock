import unittest
from datetime import datetime, timedelta, timezone

import app as app_module


CN = timezone(timedelta(hours=8))


def cn_dt(hour, minute, day=9):
    return datetime(2026, 6, day, hour, minute, tzinfo=CN)


class MarketTimeGuardTests(unittest.TestCase):
    def test_action_windows_use_china_market_time(self):
        self.assertTrue(app_module._action_in_window("scan_and_buy", cn_dt(14, 50)))
        self.assertFalse(app_module._action_in_window("scan_and_buy", cn_dt(19, 31)))

        self.assertTrue(app_module._action_in_window("track_prices", cn_dt(9, 30)))
        self.assertTrue(app_module._action_in_window("track_prices", cn_dt(10, 0)))
        self.assertFalse(app_module._action_in_window("track_prices", cn_dt(13, 47)))

        self.assertTrue(app_module._action_in_window("sell", cn_dt(10, 1)))
        self.assertFalse(app_module._action_in_window("sell", cn_dt(14, 7)))

        self.assertFalse(app_module._action_in_window("collect_trade_data", cn_dt(15, 0)))
        self.assertTrue(app_module._action_in_window("collect_trade_data", cn_dt(20, 13)))

    def test_intraday_points_do_not_relabel_wrong_times(self):
        intraday = {
            "06:20": {"000001": 10.0},
            "09:30": {"000001": 11.0},
            "10:00": {"000001": 12.0},
        }

        points = app_module._intraday_price_points(intraday, "000001")

        self.assertEqual(points["09:30"], 11.0)
        self.assertEqual(points["10:00"], 12.0)
        self.assertNotIn("09:35", points)

    def test_track_prices_writes_market_time_label(self):
        writes = {}
        auto_buy = {
            "date": "2026-06-08",
            "positions": [{"code": "000001", "name": "TEST", "buy_price": 10.0}],
        }

        class FakeResponse:
            text = 'v_sz000001="51~TEST~000001~10.5";'

        def fake_read(path):
            if path == "sim_data/auto_buy.json":
                return auto_buy, "auto-buy-sha"
            if path == "sim_data/intraday/2026-06-08.json":
                return {}, "intraday-sha"
            return None, None

        def fake_write(path, data, sha, message):
            writes[path] = data
            return True

        old_now = getattr(app_module, "_now_cn", None)
        old_read = app_module.gh_read
        old_write = app_module.gh_write
        old_get = app_module._req.get
        try:
            app_module._now_cn = lambda: cn_dt(9, 30)
            app_module.gh_read = fake_read
            app_module.gh_write = fake_write
            app_module._req.get = lambda *args, **kwargs: FakeResponse()
            with app_module.app.test_request_context(method="POST"):
                response = app_module.api_actions_track_prices()
        finally:
            if old_now is not None:
                app_module._now_cn = old_now
            app_module.gh_read = old_read
            app_module.gh_write = old_write
            app_module._req.get = old_get

        self.assertTrue(response.json["ok"])
        self.assertIn("09:30", writes["sim_data/intraday/2026-06-08.json"])
        self.assertEqual(writes["sim_data/intraday/2026-06-08.json"]["09:30"]["000001"], 10.5)

    def test_track_prices_rejects_stale_pending_buy_date(self):
        writes = {}
        auto_buy = {
            "date": "2026-06-01",
            "positions": [{"code": "000001", "name": "TEST", "buy_price": 10.0}],
        }

        class FakeResponse:
            text = 'v_sz000001="51~TEST~000001~10.5";'

        def fake_read(path):
            if path == "sim_data/auto_buy.json":
                return auto_buy, "auto-buy-sha"
            if path == "sim_data/intraday/2026-06-01.json":
                return {}, "intraday-sha"
            return None, None

        def fake_write(path, data, sha, message):
            writes[path] = data
            return True

        old_now = getattr(app_module, "_now_cn", None)
        old_read = app_module.gh_read
        old_write = app_module.gh_write
        old_get = app_module._req.get
        try:
            app_module._now_cn = lambda: cn_dt(9, 30, day=10)
            app_module.gh_read = fake_read
            app_module.gh_write = fake_write
            app_module._req.get = lambda *args, **kwargs: FakeResponse()
            with app_module.app.test_request_context(method="POST"):
                response = app_module.api_actions_track_prices()
        finally:
            if old_now is not None:
                app_module._now_cn = old_now
            app_module.gh_read = old_read
            app_module.gh_write = old_write
            app_module._req.get = old_get

        self.assertFalse(response.json["ok"])
        self.assertIn("stale pending buy date", response.json["msg"])
        self.assertEqual(writes, {})

    def test_late_sell_does_not_settle_or_clear_positions(self):
        writes = {}
        auto_buy = {
            "date": "2026-06-08",
            "positions": [{"code": "000001", "name": "TEST", "buy_price": 10.0}],
        }

        def fake_read(path):
            if path == "sim_data/auto_buy.json":
                return auto_buy, "auto-buy-sha"
            if path == "sim_data/intraday/2026-06-08.json":
                return {"10:00": {"000001": 10.5}}, "intraday-sha"
            if path == "sim_data/auto_trades.json":
                return {"trades": []}, "auto-trades-sha"
            return None, None

        def fake_write(path, data, sha, message):
            writes[path] = data
            return True

        old_now = getattr(app_module, "_now_cn", None)
        old_read = app_module.gh_read
        old_write = app_module.gh_write
        try:
            app_module._now_cn = lambda: cn_dt(14, 7)
            app_module.gh_read = fake_read
            app_module.gh_write = fake_write
            with app_module.app.test_request_context(method="POST"):
                response = app_module.api_actions_sell()
        finally:
            if old_now is not None:
                app_module._now_cn = old_now
            app_module.gh_read = old_read
            app_module.gh_write = old_write

        self.assertFalse(response.json["ok"])
        self.assertEqual(writes, {})

    def test_sell_uses_recorded_1000_intraday_price(self):
        writes = {}
        auto_buy = {
            "date": "2026-06-08",
            "positions": [{
                "code": "000001",
                "name": "TEST",
                "buy_price": 10.0,
                "industry": "bank",
                "est_win_rate": 45,
                "criteria": {"chg": True, "vol_ratio": False},
            }],
        }

        def fake_read(path):
            if path == "sim_data/auto_buy.json":
                return auto_buy, "auto-buy-sha"
            if path == "sim_data/intraday/2026-06-08.json":
                return {"10:00": {"000001": 10.5}}, "intraday-sha"
            if path == "sim_data/auto_trades.json":
                return {"trades": []}, "auto-trades-sha"
            return None, None

        def fake_write(path, data, sha, message):
            writes[path] = data
            return True

        old_now = getattr(app_module, "_now_cn", None)
        old_read = app_module.gh_read
        old_write = app_module.gh_write
        try:
            app_module._now_cn = lambda: cn_dt(10, 1)
            app_module.gh_read = fake_read
            app_module.gh_write = fake_write
            with app_module.app.test_request_context(method="POST"):
                response = app_module.api_actions_sell()
        finally:
            if old_now is not None:
                app_module._now_cn = old_now
            app_module.gh_read = old_read
            app_module.gh_write = old_write

        self.assertTrue(response.json["ok"])
        trade = writes["sim_data/auto_trades.json"]["trades"][0]
        self.assertEqual(trade["sell_price"], 10.5)
        self.assertEqual(trade["return_pct"], 4.9)
        self.assertEqual(trade["criteria"], {"chg": True, "vol_ratio": False})
        self.assertEqual(writes["sim_data/auto_buy.json"], {})

    def test_sell_rejects_stale_pending_buy_date(self):
        writes = {}
        auto_buy = {
            "date": "2026-06-01",
            "positions": [{"code": "000001", "name": "TEST", "buy_price": 10.0}],
        }

        def fake_read(path):
            if path == "sim_data/auto_buy.json":
                return auto_buy, "auto-buy-sha"
            if path == "sim_data/intraday/2026-06-01.json":
                return {"10:00": {"000001": 10.5}}, "intraday-sha"
            if path == "sim_data/auto_trades.json":
                return {"trades": []}, "auto-trades-sha"
            return None, None

        def fake_write(path, data, sha, message):
            writes[path] = data
            return True

        old_now = getattr(app_module, "_now_cn", None)
        old_read = app_module.gh_read
        old_write = app_module.gh_write
        try:
            app_module._now_cn = lambda: cn_dt(10, 1, day=10)
            app_module.gh_read = fake_read
            app_module.gh_write = fake_write
            with app_module.app.test_request_context(method="POST"):
                response = app_module.api_actions_sell()
        finally:
            if old_now is not None:
                app_module._now_cn = old_now
            app_module.gh_read = old_read
            app_module.gh_write = old_write

        self.assertFalse(response.json["ok"])
        self.assertIn("stale pending buy date", response.json["msg"])
        self.assertEqual(writes, {})

    def test_tencent_minute_points_parse_market_time_labels(self):
        class FakeResponse:
            def json(self):
                return {
                    "data": {
                        "sh600000": {
                            "data": {
                                "data": [
                                    "0930 10.10 100 1010.00",
                                    "0935 10.20 120 1224.00",
                                    "1000 10.50 200 2100.00",
                                ]
                            }
                        }
                    }
                }

        old_get = app_module._req.get
        try:
            app_module._req.get = lambda *args, **kwargs: FakeResponse()
            points = app_module._fetch_tencent_minute_points(["600000"])
        finally:
            app_module._req.get = old_get

        self.assertEqual(points["600000"]["09:30"], 10.10)
        self.assertEqual(points["600000"]["09:35"], 10.20)
        self.assertEqual(points["600000"]["10:00"], 10.50)

    def test_collect_trade_data_recovers_previous_day_pending_sell(self):
        writes = {}
        auto_buy = {
            "date": "2026-06-09",
            "positions": [{
                "code": "600000",
                "name": "TEST",
                "buy_price": 10.0,
                "industry": "bank",
                "est_win_rate": 45,
                "criteria": {"chg": True},
            }],
        }
        snapshot = {
            "snapshot_time": "10:25:48",
            "snapshot_timezone": "Asia/Shanghai",
            "snapshots": {
                "600000": {
                    "name": "TEST",
                    "industry": "bank",
                    "close": 10.0,
                    "est_win_rate": 45,
                    "criteria": {"chg": True},
                }
            }
        }

        def fake_read(path):
            if path in writes:
                return writes[path], f"{path}-sha"
            if path == "sim_data/auto_buy.json":
                return auto_buy, "auto-buy-sha"
            if path == "sim_data/auto_trades.json":
                return {"trades": []}, "auto-trades-sha"
            if path == "sim_data/buy_snapshots/2026-06-09.json":
                return snapshot, "snapshot-sha"
            if path == "sim_data/intraday/2026-06-09.json":
                return {}, "intraday-sha"
            if path == "sim_data/trade_details/2026-06-10.json":
                return None, None
            return None, None

        def fake_write(path, data, sha, message):
            writes[path] = data
            return True

        def fake_minute_points(codes):
            self.assertEqual(codes, ["600000"])
            return {
                "600000": {
                    "09:30": 10.2,
                    "09:35": 10.3,
                    "09:40": 10.4,
                    "09:45": 10.45,
                    "09:50": 10.48,
                    "09:55": 10.49,
                    "10:00": 10.5,
                }
            }

        old_now = app_module._now_cn
        old_read = app_module.gh_read
        old_write = app_module.gh_write
        old_minutes = getattr(app_module, "_fetch_tencent_minute_points", None)
        try:
            app_module._now_cn = lambda: cn_dt(15, 30, day=10)
            app_module.gh_read = fake_read
            app_module.gh_write = fake_write
            app_module._fetch_tencent_minute_points = fake_minute_points
            with app_module.app.test_request_context(method="POST"):
                response = app_module.api_actions_collect_trade_data()
        finally:
            app_module._now_cn = old_now
            app_module.gh_read = old_read
            app_module.gh_write = old_write
            if old_minutes is not None:
                app_module._fetch_tencent_minute_points = old_minutes
            elif hasattr(app_module, "_fetch_tencent_minute_points"):
                delattr(app_module, "_fetch_tencent_minute_points")

        self.assertTrue(response.json["ok"])
        settled = writes["sim_data/auto_trades.json"]["trades"][0]
        self.assertEqual(settled["buy_date"], "2026-06-09")
        self.assertEqual(settled["sell_date"], "2026-06-10")
        self.assertEqual(settled["sell_price"], 10.5)
        self.assertEqual(settled["return_pct"], 4.9)
        self.assertEqual(settled["criteria"], {"chg": True})
        self.assertEqual(writes["sim_data/auto_buy.json"], {})
        self.assertEqual(writes["sim_data/intraday/2026-06-09.json"]["10:00"]["600000"], 10.5)
        detail = writes["sim_data/trade_details/2026-06-10.json"]["records"][0]
        self.assertEqual(detail["buy_date"], "2026-06-09")
        self.assertEqual(detail["sell_date"], "2026-06-10")
        self.assertEqual(detail["criteria"], {"chg": True})
        self.assertEqual(detail["price_1000"], 10.5)
        self.assertEqual(detail["snapshot_time"], "10:25:48")
        self.assertEqual(detail["snapshot_timezone"], "Asia/Shanghai")
        self.assertEqual(detail["snapshot_at"], "buy_1025")

    def test_collect_trade_data_writes_empty_audit_when_no_positions(self):
        writes = {}

        def fake_read(path):
            if path == "sim_data/auto_trades.json":
                return {"trades": []}, "auto-trades-sha"
            if path == "sim_data/auto_buy.json":
                return {}, "auto-buy-sha"
            if path == "sim_data/trade_details/2026-06-10.json":
                return None, None
            return None, None

        def fake_write(path, data, sha, message):
            writes[path] = data
            return True

        old_now = app_module._now_cn
        old_read = app_module.gh_read
        old_write = app_module.gh_write
        try:
            app_module._now_cn = lambda: cn_dt(15, 30, day=10)
            app_module.gh_read = fake_read
            app_module.gh_write = fake_write
            with app_module.app.test_request_context(method="POST"):
                response = app_module.api_actions_collect_trade_data()
        finally:
            app_module._now_cn = old_now
            app_module.gh_read = old_read
            app_module.gh_write = old_write

        self.assertTrue(response.json["ok"])
        self.assertEqual(response.json["status"], "no_trades")
        self.assertEqual(response.json["count"], 0)
        detail = writes["sim_data/trade_details/2026-06-10.json"]
        self.assertEqual(detail["status"], "no_trades")
        self.assertEqual(detail["reason"], "no pending positions")
        self.assertEqual(detail["records"], [])


if __name__ == "__main__":
    unittest.main()
