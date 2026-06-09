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


if __name__ == "__main__":
    unittest.main()
