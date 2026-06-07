from app import build_signal_snapshot_records


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


if __name__ == "__main__":
    test_overnight_signal_snapshot_uses_1450_top5_strategy()
    print("[PASS] test_overnight_signal_snapshot_uses_1450_top5_strategy")
