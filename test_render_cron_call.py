import unittest

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


if __name__ == "__main__":
    unittest.main()
