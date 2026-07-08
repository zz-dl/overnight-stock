import app
from app import build_signal_snapshot_doc, build_signal_snapshot_records


def test_overnight_signal_snapshot_uses_1450_top5_strategy():
    today = "2026-06-07"
    stocks = [{
        "code": "000001",
        "name": "S0",
        "price": 12.3,
        "industry": "bank",
        "criteria": {"chg": True, "turnover": True},
        "est_win_rate": 0.63,
        "chg_pct": 3.2,
        "vol_ratio": 2.1,
        "turnover_rate": 4.4,
    }]
    result = {"market_win_rate": 0.58, "index_chg": 0.7}

    records = build_signal_snapshot_records(today, stocks, result)

    assert len(records) == 1
    rec = records[0]
    assert rec["source_app"] == "overnight_stock"
    assert rec["strategy"] == "overnight_top5_1450"
    assert rec["snapshot_date"] == today
    assert rec["snapshot_time"] == "14:50:00"
    assert rec["signal_action"] == "buy_top5"
    assert rec["rank"] == 1
    assert rec["price"] == 12.3
    assert rec["score"] == 0.63
    assert rec["factors"]["criteria"]["chg"] is True
    assert rec["forward_returns"]["next_open_pct"] is None


def test_empty_signal_snapshot_keeps_no_buy_diagnostics():
    doc = build_signal_snapshot_doc(
        "2026-07-08",
        [],
        {
            "index_chg": -0.49,
            "market_win_rate": 38,
            "market_condition": "大盘-0.49%，未达到买入门槛",
            "above_ma250": True,
            "total_scanned": 0,
            "total_found": 0,
            "scan_time": "14:50:02",
            "active_criteria": [],
        },
    )

    assert doc["records"] == []
    assert doc["no_buy_reason"] == "大盘-0.49%，未达到买入门槛"
    assert doc["index_chg"] == -0.49
    assert doc["total_scanned"] == 0
    assert doc["total_found"] == 0
    assert doc["scan_time"] == "14:50:02"


def test_fetch_industry_uses_f100_when_f127_is_numeric():
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if "stock/get" in url:
            return FakeResponse({"data": {"f127": 8.84, "f128": "-", "f129": 1.61}})
        if "ulist.np/get" in url:
            return FakeResponse({"data": {"diff": [{"f100": "Optics", "f102": "Anhui"}]}})
        raise AssertionError(url)

    original_get = app._req.get
    try:
        app._req.get = fake_get
        assert app._fetch_industry("600552") == "Optics"
    finally:
        app._req.get = original_get

    assert any("stock/get" in url for url in calls)
    assert any("ulist.np/get" in url for url in calls)


if __name__ == "__main__":
    test_overnight_signal_snapshot_uses_1450_top5_strategy()
    test_empty_signal_snapshot_keeps_no_buy_diagnostics()
    test_fetch_industry_uses_f100_when_f127_is_numeric()
    print("[PASS] test_overnight_signal_snapshot_uses_1450_top5_strategy")
    print("[PASS] test_empty_signal_snapshot_keeps_no_buy_diagnostics")
    print("[PASS] test_fetch_industry_uses_f100_when_f127_is_numeric")
