"""Tests for 一夜持股 backtest engine (TDD — written before implementation)."""

import os, unittest
import numpy as np

from backtest import read_day_file, calc_criteria, analyze_results

DATA_DIR = r"F:\OvernightStock\download\hsjday"
SH600519 = os.path.join(DATA_DIR, "sh", "lday", "sh600519.day")  # 贵州茅台


class TestReadDayFile(unittest.TestCase):

    def test_reads_correct_number_of_records_for_moutai(self):
        df = read_day_file(SH600519)
        self.assertEqual(len(df), 5929)

    def test_last_record_date_and_close(self):
        df = read_day_file(SH600519)
        last = df.iloc[-1]
        self.assertEqual(last["date"], 20260529)
        self.assertAlmostEqual(last["close"], 1326.0, places=1)

    def test_first_record_date(self):
        df = read_day_file(SH600519)
        self.assertEqual(df.iloc[0]["date"], 20010827)

    def test_ohlc_columns_exist(self):
        df = read_day_file(SH600519)
        for col in ("date", "open", "high", "low", "close", "vol", "amount"):
            self.assertIn(col, df.columns)

    def test_close_prices_are_positive(self):
        df = read_day_file(SH600519)
        self.assertTrue((df["close"] > 0).all())

    def test_high_ge_low_always(self):
        df = read_day_file(SH600519)
        self.assertTrue((df["high"] >= df["low"]).all())


class TestCalcCriteria(unittest.TestCase):
    """
    calc_criteria(close, prev_close, high_20d, index_chg, vol_today, vol_5d_avg)
    → dict with keys: chg, vol_ratio, limit_gene, stronger
    """

    def test_stock_up_4pct_passes_chg(self):
        c = calc_criteria(close=104, prev_close=100, high_20d=[],
                          index_chg=2.0, vol_today=150, vol_5d_avg=100)
        self.assertTrue(c["chg"])

    def test_stock_up_2pct_fails_chg(self):
        c = calc_criteria(close=102, prev_close=100, high_20d=[],
                          index_chg=1.0, vol_today=150, vol_5d_avg=100)
        self.assertFalse(c["chg"])

    def test_stock_up_6pct_fails_chg(self):
        c = calc_criteria(close=106, prev_close=100, high_20d=[],
                          index_chg=1.0, vol_today=150, vol_5d_avg=100)
        self.assertFalse(c["chg"])

    def test_vol_ratio_above_1_passes(self):
        c = calc_criteria(close=104, prev_close=100, high_20d=[],
                          index_chg=1.0, vol_today=200, vol_5d_avg=100)
        self.assertTrue(c["vol_ratio"])

    def test_vol_ratio_below_1_fails(self):
        c = calc_criteria(close=104, prev_close=100, high_20d=[],
                          index_chg=1.0, vol_today=80, vol_5d_avg=100)
        self.assertFalse(c["vol_ratio"])

    def test_criteria_does_not_include_limit_gene(self):
        # 涨停基因已从策略移除（回测证明为负效应），不应出现在结果中
        c = calc_criteria(close=104, prev_close=100, high_20d=[],
                          index_chg=1.0, vol_today=100, vol_5d_avg=100)
        self.assertNotIn("limit_gene", c)

    def test_stronger_than_market_passes(self):
        c = calc_criteria(close=104, prev_close=100, high_20d=[],
                          index_chg=2.0, vol_today=100, vol_5d_avg=100)
        self.assertTrue(c["stronger"])   # stock +4% > index +2%

    def test_weaker_than_market_fails(self):
        c = calc_criteria(close=103, prev_close=100, high_20d=[],
                          index_chg=4.0, vol_today=100, vol_5d_avg=100)
        self.assertFalse(c["stronger"])  # stock +3% < index +4%


class TestAnalyzeResults(unittest.TestCase):
    """
    analyze_results(trades) → dict with overall stats + per-criterion stats
    trades: list of dicts with keys: return_pct, criteria {chg, vol_ratio, limit_gene, stronger}
    """

    def _trade(self, ret, **criteria):
        base = {"chg": True, "vol_ratio": True, "limit_gene": False, "stronger": True}
        base.update(criteria)
        return {"return_pct": ret, "criteria": base}

    def test_all_winning_trades_give_100pct_win_rate(self):
        trades = [self._trade(2.0), self._trade(1.5), self._trade(3.0)]
        result = analyze_results(trades)
        self.assertAlmostEqual(result["overall"]["win_rate"], 1.0)

    def test_all_losing_trades_give_0pct_win_rate(self):
        trades = [self._trade(-1.0), self._trade(-2.0)]
        result = analyze_results(trades)
        self.assertAlmostEqual(result["overall"]["win_rate"], 0.0)

    def test_mixed_trades_correct_win_rate(self):
        trades = [self._trade(2.0), self._trade(-1.0), self._trade(1.0), self._trade(-0.5)]
        result = analyze_results(trades)
        self.assertAlmostEqual(result["overall"]["win_rate"], 0.5)

    def test_avg_return_computed_correctly(self):
        trades = [self._trade(4.0), self._trade(-2.0)]
        result = analyze_results(trades)
        self.assertAlmostEqual(result["overall"]["avg_return"], 1.0)

    def test_per_criterion_win_rate_for_limit_gene(self):
        # 2 trades where limit_gene passed: 1 win, 1 loss → 50% win rate
        trades = [
            self._trade(2.0,  limit_gene=True),
            self._trade(-1.0, limit_gene=True),
            self._trade(3.0,  limit_gene=False),  # not counted for limit_gene
        ]
        result = analyze_results(trades)
        self.assertAlmostEqual(result["criteria"]["limit_gene"]["win_rate"], 0.5)

    def test_criterion_not_passed_in_any_trade_returns_none(self):
        trades = [self._trade(1.0, stronger=False), self._trade(2.0, stronger=False)]
        result = analyze_results(trades)
        # 'stronger' never passed → None or 0
        wr = result["criteria"]["stronger"]["win_rate"]
        self.assertIsNone(wr)

    def test_total_trades_count(self):
        trades = [self._trade(1.0), self._trade(-1.0), self._trade(2.0)]
        result = analyze_results(trades)
        self.assertEqual(result["overall"]["total"], 3)


if __name__ == "__main__":
    unittest.main()


# ── 胜率计算测试 ──────────────────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(__file__))
from app import calc_market_win_rate, calc_stock_win_rate, build_reasons

class TestWinRate(unittest.TestCase):

    def test_market_win_rate_sweet_spot(self):
        wr = calc_market_win_rate(index_chg=0.72, weekday=2, consec_up=2)
        self.assertGreaterEqual(wr, 50)
        self.assertLessEqual(wr, 72)

    def test_market_win_rate_weak_market(self):
        wr = calc_market_win_rate(index_chg=0.2, weekday=1, consec_up=1)
        self.assertLess(wr, 45)

    def test_stock_win_rate_adds_excess_bonus(self):
        base = calc_market_win_rate(0.72, 2, 1)
        with_optimal_excess = calc_stock_win_rate(base, excess=2.1)
        self.assertGreater(with_optimal_excess, base)

    def test_stock_win_rate_penalizes_high_excess(self):
        base = calc_market_win_rate(0.72, 2, 1)
        high_excess = calc_stock_win_rate(base, excess=5.0)
        self.assertLess(high_excess, base)

    def test_build_reasons_contains_key_info(self):
        reasons = build_reasons(chg_pct=4.3, index_chg=0.72, excess=2.1,
                                market_win_rate=54)
        self.assertEqual(len(reasons), 3)
        self.assertTrue(any("4.3" in r for r in reasons))
        self.assertTrue(any("2.1" in r for r in reasons))
        self.assertTrue(any("54" in r for r in reasons))

    def test_win_rate_clamped(self):
        lo = calc_market_win_rate(0.1, 3, 0)
        hi = calc_market_win_rate(3.0, 4, 5)
        self.assertGreaterEqual(lo, 30)
        self.assertLessEqual(hi, 72)
