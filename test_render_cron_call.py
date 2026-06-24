import unittest
from datetime import datetime, timedelta, timezone

from scripts import render_cron_call


class FakeResponse:
    def __init__(self, status_code=200, text='{"ok":false}'):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RenderCronCallTests(unittest.TestCase):
    def test_build_url_uses_known_action_path(self):
        url = render_cron_call.build_url("track-prices", "https://overnight-stock.onrender.com/")
        self.assertEqual(
            url,
            "https://overnight-stock.onrender.com/api/actions/track-prices",
        )

    def test_call_action_accepts_business_false_response(self):
        calls = []

        def fake_post(url, headers, timeout):
            calls.append((url, headers, timeout))
            return FakeResponse(200, '{"ok":false,"msg":"no positions"}')

        code, body = render_cron_call.call_action(
            "sell",
            base_url="https://example.test",
            post=fake_post,
        )

        self.assertEqual(code, 0)
        self.assertIn("no positions", body)
        self.assertEqual(calls[0][0], "https://example.test/api/actions/sell")

    def test_call_action_fails_on_outside_window_response(self):
        def fake_post(url, headers, timeout):
            return FakeResponse(200, '{"ok":false,"msg":"outside sell window"}')

        code, body = render_cron_call.call_action(
            "sell",
            base_url="https://example.test",
            post=fake_post,
        )

        self.assertEqual(code, 1)
        self.assertIn("outside sell window", body)

    def test_call_action_fails_on_http_error(self):
        def fake_post(url, headers, timeout):
            return FakeResponse(500, "server error")

        code, body = render_cron_call.call_action(
            "sell",
            base_url="https://example.test",
            post=fake_post,
        )

        self.assertEqual(code, 1)
        self.assertIn("server error", body)

    def test_wait_and_call_sleeps_until_target_market_time(self):
        sleeps = []
        calls = []
        now = datetime(2026, 6, 24, 14, 40, tzinfo=timezone(timedelta(hours=8)))

        def fake_call(action, base_url=None):
            calls.append((action, base_url))
            return 0, '{"ok":true}'

        code, body = render_cron_call.wait_and_call(
            "scan-and-buy",
            "14:50",
            "15:00",
            base_url="https://example.test",
            now=lambda: now,
            sleep=sleeps.append,
            call=fake_call,
        )

        self.assertEqual(code, 0)
        self.assertEqual(body, '{"ok":true}')
        self.assertEqual(sleeps, [600])
        self.assertEqual(calls, [("scan-and-buy", "https://example.test")])

    def test_wait_and_call_fails_after_deadline_without_calling(self):
        calls = []
        now = datetime(2026, 6, 24, 15, 1, tzinfo=timezone(timedelta(hours=8)))

        code, body = render_cron_call.wait_and_call(
            "scan-and-buy",
            "14:50",
            "15:00",
            now=lambda: now,
            sleep=lambda seconds: None,
            call=lambda action, base_url=None: calls.append(action),
        )

        self.assertEqual(code, 1)
        self.assertIn("missed scan-and-buy window", body)
        self.assertEqual(calls, [])

    def test_track_price_session_waits_and_calls_each_label(self):
        class FakeClock:
            def __init__(self):
                self.current = datetime(2026, 6, 24, 9, 29, tzinfo=timezone(timedelta(hours=8)))
                self.sleeps = []

            def now(self):
                return self.current

            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.current += timedelta(seconds=seconds)

        clock = FakeClock()
        calls = []

        def fake_call(action, base_url=None):
            calls.append((action, clock.now().strftime("%H:%M")))
            return 0, '{"ok":false,"msg":"no positions"}'

        code, body = render_cron_call.track_price_session(
            labels=("09:30", "09:35"),
            now=clock.now,
            sleep=clock.sleep,
            call=fake_call,
        )

        self.assertEqual(code, 0)
        self.assertEqual(body, "track session complete")
        self.assertEqual(clock.sleeps, [60, 300])
        self.assertEqual(calls, [("track-prices", "09:30"), ("track-prices", "09:35")])


if __name__ == "__main__":
    unittest.main()
