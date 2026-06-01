"""Tests for simulation trade logic — written BEFORE implementation (TDD)."""

import json, unittest
from unittest.mock import patch, MagicMock

# ── 被测函数（尚未存在，测试先行）──────────────────────────────────────
from sim import (
    settle_trades,
    update_criteria_stats,
    auto_adjust_criteria,
    CRITERIA_KEYS,
)


class TestSettleTrades(unittest.TestCase):

    def _pending(self, positions):
        return {"date": "2026-05-31", "scan_time": "14:52:00", "positions": positions}

    def test_profit_trade_recorded_correctly(self):
        pending = self._pending([{
            "code": "000001", "name": "平安银行", "price": 10.0,
            "criteria": {"chg": True, "turnover": True, "vol_ratio": True,
                         "cap": True, "limit_gene": False, "vwap": True, "stronger": True},
            "score": 6,
        }])
        prices = {"000001": 10.5}

        trades = settle_trades(pending, prices, sell_date="2026-06-01")

        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["code"], "000001")
        self.assertAlmostEqual(t["return_pct"], 5.0, places=1)
        self.assertEqual(t["sell_date"], "2026-06-01")
        self.assertEqual(t["buy_date"], "2026-05-31")

    def test_loss_trade_recorded_with_negative_return(self):
        pending = self._pending([{
            "code": "600519", "name": "贵州茅台", "price": 1300.0,
            "criteria": {"chg": True, "turnover": False, "vol_ratio": True,
                         "cap": False, "limit_gene": True, "vwap": True, "stronger": True},
            "score": 5,
        }])
        prices = {"600519": 1274.0}

        trades = settle_trades(pending, prices, sell_date="2026-06-01")

        self.assertEqual(len(trades), 1)
        self.assertLess(trades[0]["return_pct"], 0)

    def test_stock_with_no_price_is_skipped(self):
        pending = self._pending([
            {"code": "000001", "name": "A", "price": 10.0,
             "criteria": {k: True for k in CRITERIA_KEYS}, "score": 7},
            {"code": "MISSING", "name": "B", "price": 5.0,
             "criteria": {k: True for k in CRITERIA_KEYS}, "score": 7},
        ])
        prices = {"000001": 10.5}  # MISSING has no price

        trades = settle_trades(pending, prices, sell_date="2026-06-01")

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["code"], "000001")

    def test_empty_pending_returns_empty_list(self):
        pending = self._pending([])
        trades = settle_trades(pending, {}, sell_date="2026-06-01")
        self.assertEqual(trades, [])


class TestUpdateCriteriaStats(unittest.TestCase):

    def _stats(self, active=None):
        active = active or list(CRITERIA_KEYS)
        return {
            "active_criteria": active,
            "stats": {k: {"wins": 0, "losses": 0, "win_rate": 0.0, "active": True}
                      for k in CRITERIA_KEYS},
        }

    def test_winning_trade_increments_win_for_passed_criteria(self):
        stats = self._stats()
        trade = {
            "return_pct": 3.0,
            "criteria": {"chg": True, "turnover": True, "vol_ratio": False,
                         "cap": True, "limit_gene": False, "vwap": True, "stronger": True},
        }
        result = update_criteria_stats(stats, [trade])

        self.assertEqual(result["stats"]["chg"]["wins"], 1)
        self.assertEqual(result["stats"]["chg"]["losses"], 0)
        self.assertEqual(result["stats"]["vol_ratio"]["wins"], 0)  # not passed

    def test_losing_trade_increments_loss_for_passed_criteria(self):
        stats = self._stats()
        trade = {
            "return_pct": -2.0,
            "criteria": {"chg": True, "turnover": True, "vol_ratio": True,
                         "cap": True, "limit_gene": True, "vwap": True, "stronger": True},
        }
        result = update_criteria_stats(stats, [trade])

        for k in CRITERIA_KEYS:
            self.assertEqual(result["stats"][k]["losses"], 1)
            self.assertEqual(result["stats"][k]["wins"], 0)

    def test_win_rate_computed_correctly(self):
        stats = self._stats()
        # 6 wins, 4 losses for "chg"
        trades = []
        for i in range(6):
            trades.append({"return_pct": 1.0,
                           "criteria": {k: (k == "chg") for k in CRITERIA_KEYS}})
        for i in range(4):
            trades.append({"return_pct": -1.0,
                           "criteria": {k: (k == "chg") for k in CRITERIA_KEYS}})

        result = update_criteria_stats(stats, trades)

        self.assertAlmostEqual(result["stats"]["chg"]["win_rate"], 0.6, places=2)

    def test_rolling_window_capped_at_30(self):
        stats = self._stats()
        # prime with 30 wins
        stats["stats"]["chg"]["wins"] = 30
        stats["stats"]["chg"]["losses"] = 0
        stats["stats"]["chg"]["win_rate"] = 1.0

        # add 1 loss — window should not exceed 30 total
        trade = {"return_pct": -1.0,
                 "criteria": {k: (k == "chg") for k in CRITERIA_KEYS}}
        result = update_criteria_stats(stats, [trade])

        total = result["stats"]["chg"]["wins"] + result["stats"]["chg"]["losses"]
        self.assertLessEqual(total, 30)


class TestAutoAdjustCriteria(unittest.TestCase):

    def _stats(self, rates: dict):
        """rates: {criterion: win_rate}. All start active."""
        return {
            "active_criteria": list(CRITERIA_KEYS),
            "stats": {
                k: {"wins": 0, "losses": 0, "win_rate": rates.get(k, 0.5), "active": True}
                for k in CRITERIA_KEYS
            },
        }

    def test_criterion_below_40pct_gets_deactivated(self):
        stats = self._stats({"vwap": 0.35})
        result = auto_adjust_criteria(stats)
        self.assertFalse(result["stats"]["vwap"]["active"])
        self.assertNotIn("vwap", result["active_criteria"])

    def test_criterion_above_50pct_gets_reactivated(self):
        stats = self._stats({"vwap": 0.55})
        stats["stats"]["vwap"]["active"] = False
        stats["active_criteria"].remove("vwap")

        result = auto_adjust_criteria(stats)

        self.assertTrue(result["stats"]["vwap"]["active"])
        self.assertIn("vwap", result["active_criteria"])

    def test_never_deactivates_below_3_active(self):
        # Only 3 criteria left, all with bad win rates
        bad_rates = {k: 0.30 for k in CRITERIA_KEYS}
        stats = self._stats(bad_rates)

        result = auto_adjust_criteria(stats)

        self.assertGreaterEqual(len(result["active_criteria"]), 3)

    def test_healthy_criterion_stays_active(self):
        stats = self._stats({"chg": 0.65})
        result = auto_adjust_criteria(stats)
        self.assertTrue(result["stats"]["chg"]["active"])
        self.assertIn("chg", result["active_criteria"])


if __name__ == "__main__":
    unittest.main()
