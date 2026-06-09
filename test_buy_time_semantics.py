import json
from pathlib import Path

import app as app_module


ROOT = Path(__file__).resolve().parent
EXPECTED_BUY_TIME = "14:50:00"


def check(label, cond, detail=""):
    mark = "[PASS]" if cond else "[FAIL]"
    print(f"  {mark} {label}" + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise AssertionError(label)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_sell_endpoint_normalizes_legacy_buy_time():
    writes = {}
    auto_buy = {
        "date": "2026-06-05",
        "buy_time": "10:38:28",
        "positions": [{
            "code": "000012",
            "name": "TEST",
            "buy_price": 4.38,
            "industry": "glass",
            "est_win_rate": 35,
        }],
    }

    def fake_read(path):
        if path == "sim_data/auto_buy.json":
            return auto_buy, "auto-buy-sha"
        if path == "sim_data/intraday/2026-06-05.json":
            return {"10:00": {"000012": 4.29}}, "intraday-sha"
        if path == "sim_data/auto_trades.json":
            return {"trades": []}, "auto-trades-sha"
        return None, None

    def fake_write(path, data, sha, message):
        writes[path] = data
        return True

    old_read = app_module.gh_read
    old_write = app_module.gh_write
    old_now = app_module._now_cn
    try:
        app_module.gh_read = fake_read
        app_module.gh_write = fake_write
        app_module._now_cn = lambda: app_module.datetime(2026, 6, 8, 10, 1, tzinfo=app_module.MARKET_TZ)
        with app_module.app.test_request_context(method="POST"):
            response = app_module.api_actions_sell()
    finally:
        app_module.gh_read = old_read
        app_module.gh_write = old_write
        app_module._now_cn = old_now

    check("sell endpoint succeeds with legacy buy_time", response.json["ok"])
    written = writes["sim_data/auto_trades.json"]["trades"][0]
    check(
        "sell endpoint writes strategy buy time",
        written.get("buy_time") == EXPECTED_BUY_TIME,
        f"={written.get('buy_time')!r}",
    )


test_sell_endpoint_normalizes_legacy_buy_time()

bad_auto_trades = []
auto_trades = read_json(ROOT / "sim_data" / "auto_trades.json")
for trade in auto_trades.get("trades", []):
    if trade.get("buy_time") != EXPECTED_BUY_TIME:
        bad_auto_trades.append(
            f"{trade.get('sell_date')} {trade.get('code')} buy_time={trade.get('buy_time')!r}"
        )

check(
    "auto_trades use strategy buy time",
    not bad_auto_trades,
    "; ".join(bad_auto_trades[:8]),
)

bad_trade_details = []
for path in sorted((ROOT / "sim_data" / "trade_details").glob("*.json")):
    data = read_json(path)
    for record in data.get("records", []):
        if record.get("buy_time") != EXPECTED_BUY_TIME:
            bad_trade_details.append(
                f"{path.name} {record.get('code')} buy_time={record.get('buy_time')!r}"
            )

check(
    "trade details use strategy buy time",
    not bad_trade_details,
    "; ".join(bad_trade_details[:8]),
)

print("ALL TESTS PASSED")
