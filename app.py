"""一夜持股法选股助手 — 杨永兴七大硬标准扫描器"""

from __future__ import annotations

import concurrent.futures
import re
import socket
import threading
import time
from datetime import datetime, date, timedelta
from math import isnan
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, send_from_directory

from sim import (
    gh_read, gh_write, settle_trades,
    update_criteria_stats, auto_adjust_criteria,
    _default_stats, CRITERIA_KEYS,
)

socket.setdefaulttimeout(8)

app = Flask(__name__, static_folder="static")
STRATEGY_BUY_TIME = "14:50:00"
MARKET_TZ = ZoneInfo("Asia/Shanghai")
ACTION_WINDOWS = {
    "track_prices": ((9, 30), (10, 0)),
    "sell": ((9, 58), (10, 10)),
    "scan_and_buy": ((14, 45), (15, 0)),
    "collect_trade_data": ((15, 25), (23, 59)),
}

_req = requests.Session()
_req.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://finance.qq.com",
})


def safe_float(v, default=0.0):
    try:
        f = float(v)
        return default if isnan(f) else f
    except Exception:
        return default


def _now_cn() -> datetime:
    return datetime.now(MARKET_TZ)


def _today_cn() -> date:
    return _now_cn().date()


def _hm_value(hm: tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


def _action_in_window(action: str, now_cn: datetime | None = None) -> bool:
    now_cn = now_cn or _now_cn()
    if now_cn.tzinfo is not None:
        now_cn = now_cn.astimezone(MARKET_TZ)
    window = ACTION_WINDOWS.get(action)
    if not window or now_cn.weekday() >= 5:
        return False
    current = now_cn.hour * 60 + now_cn.minute
    start, end = window
    return _hm_value(start) <= current <= _hm_value(end)


def _outside_window_response(action: str, now_cn: datetime | None = None):
    now_cn = now_cn or _now_cn()
    if now_cn.tzinfo is not None:
        now_cn = now_cn.astimezone(MARKET_TZ)
    return jsonify({
        "ok": False,
        "msg": f"outside {action} window",
        "date": now_cn.date().isoformat(),
        "market_time": now_cn.strftime("%H:%M:%S"),
    })


# Signal snapshots for strategy-level evaluation.
def build_signal_snapshot_records(today: str, stocks: list[dict], result: dict | None = None) -> list[dict]:
    """Capture the 14:50 Top5 candidates before the next-morning sell check."""
    result = result or {}
    records: list[dict] = []
    for i, s in enumerate(stocks or [], 1):
        criteria = s.get("criteria", {})
        records.append({
            "source_app": "overnight_stock",
            "strategy": "overnight_top5_1450",
            "snapshot_date": today,
            "snapshot_time": STRATEGY_BUY_TIME,
            "sequence": i,
            "code": s.get("code", ""),
            "name": s.get("name", s.get("code", "")),
            "market": "A",
            "industry": s.get("industry", ""),
            "rank": i,
            "score": s.get("est_win_rate") or s.get("score"),
            "recommendation": "buy",
            "signal_action": "buy_top5",
            "price": s.get("price") or s.get("buy_price"),
            "open": s.get("open"),
            "close": s.get("close"),
            "chg_pct": s.get("chg_pct") or s.get("f3"),
            "vol_ratio": s.get("vol_ratio") or s.get("f10"),
            "turnover": s.get("turnover_rate") or s.get("turnover"),
            "capital_net": s.get("main_net"),
            "market_state": {
                "market_win_rate": result.get("market_win_rate"),
                "index_chg": result.get("index_chg"),
            },
            "weights": {},
            "factors": {"criteria": criteria},
            "news": s.get("news", []),
            "raw": s,
            "forward_returns": {
                "next_open_pct": None,
                "next_close_pct": None,
                "return_5d_pct": None,
                "return_20d_pct": None,
            },
        })
    return records


def _nullable_float(value):
    if value is None:
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return None if isnan(parsed) else parsed


def _nullable_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _no_buy_reason(result: dict | None) -> str:
    result = result or {}
    for key in ("market_condition", "error", "msg"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "no candidates"


def build_signal_snapshot_doc(today: str, stocks: list[dict], result: dict | None = None) -> dict:
    """Build a persisted snapshot with diagnostics even when the buy list is empty."""
    result = result or {}
    records = build_signal_snapshot_records(today, stocks, result)
    doc = {
        "date": today,
        "source_app": "overnight_stock",
        "strategy": "overnight_top5_1450",
        "snapshot_time": STRATEGY_BUY_TIME,
        "scan_time": result.get("scan_time"),
        "index_chg": _nullable_float(result.get("index_chg")),
        "market_win_rate": _nullable_int(result.get("market_win_rate")),
        "market_condition": result.get("market_condition"),
        "above_ma250": result.get("above_ma250"),
        "total_scanned": _nullable_int(result.get("total_scanned")),
        "total_found": _nullable_int(result.get("total_found")),
        "active_criteria": result.get("active_criteria", []),
        "records": records,
    }
    if not records:
        doc["no_buy_reason"] = _no_buy_reason(result)
    return doc


# ── 上证指数涨跌幅 ─────────────────────────────────────────────
def get_index_chg() -> float:
    try:
        r = _req.get("http://qt.gtimg.cn/q=sh000001", timeout=5)
        parts = r.text.split("~")
        return safe_float(parts[32]) if len(parts) > 32 else 0.0
    except Exception:
        return 0.0


def get_index_above_ma250() -> bool:
    """
    判断上证指数当前收盘是否在250日均线（年线）上方。
    年线下方 = 结构性熊市，策略应暂停。
    获取失败时默认返回 True（允许操作，避免误拦截）。
    """
    try:
        r = _req.get(
            "http://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": "1.000001",
                "fields1": "f1,f2,f3,f4,f5",
                "fields2": "f51,f52,f53,f54,f55",
                "klt": 101, "fqt": 0, "end": "20500101", "lmt": 260,
            },
            timeout=8,
        )
        klines = (r.json().get("data") or {}).get("klines") or []
        if len(klines) < 250:
            return True
        closes = [safe_float(k.split(",")[2]) for k in klines[-250:]]
        closes = [c for c in closes if c > 0]
        if len(closes) < 200:
            return True
        ma250 = sum(closes) / len(closes)
        current = closes[-1]
        return current > ma250
    except Exception:
        return True  # 获取失败：默认允许，不误拦截


# ── A 股代码段 ────────────────────────────────────────────────
def _gen_astock_qtcodes() -> list[str]:
    """
    回测结论①：只做沪市 → 只生成 sh 代码段（不再扫描深市/创业板）。
    ~1600 只：沪主板核心 + 沪主板扩展 + 科创板核心。
    （只扫活跃代码段，避免触发腾讯反爬。）
    """
    pairs: list[str] = []
    for n in range(600000, 600600):   # 沪主板核心（600000-600599）
        pairs.append(f"sh{n}")
    for n in range(601000, 601400):   # 沪主板扩展（601000-601399）
        pairs.append(f"sh{n}")
    for n in range(603000, 603300):   # 沪主板扩展（603000-603299）
        pairs.append(f"sh{n}")
    for n in range(688000, 688300):   # 科创板核心（688000-688299）
        pairs.append(f"sh{n}")
    return pairs


# ── 腾讯行情批量扫描（ThreadPoolExecutor，真正并发）──────────────
def _tencent_batch_scan(qtcodes: list[str], chg_min: float = 2.5) -> list[dict]:
    """
    限速扫描：BATCH=50, WORKERS=5，避免触发腾讯反爬。
    ~2200 codes / 50 = 44 批次，5 线程 ≈ 9 轮 × ~2s = ~18s。
    """
    BATCH = 50
    WORKERS = 5
    results: list[dict] = []
    lock = threading.Lock()

    def fetch(batch: list[str]) -> None:
        try:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                "Referer": "https://finance.qq.com",
            })
            r = sess.get(f"http://qt.gtimg.cn/q={','.join(batch)}", timeout=(5, 8))
            for seg in r.text.strip().split(";"):
                if "~" not in seg or "=" not in seg:
                    continue
                m = re.search(r'v_(\w+)="([^"]+)"', seg)
                if not m:
                    continue
                qtcode = m.group(1)
                parts = m.group(2).split("~")
                if len(parts) < 40:
                    continue
                price = safe_float(parts[3])
                if price <= 0:
                    continue
                chg_pct = safe_float(parts[32])
                if chg_pct < chg_min:
                    continue
                name = parts[1]
                if not name or "ST" in name.upper():
                    continue
                volume = safe_float(parts[36])
                amount = safe_float(parts[37])
                turnover = safe_float(parts[38])
                vol_ratio = (
                    safe_float(parts[49])
                    if len(parts) > 49 and 0 < safe_float(parts[49]) < 30
                    else 0.0
                )
                float_cap = volume * 10000 * price / (turnover * 1e8) if turnover > 0.01 else 0.0
                prefix = qtcode[:2]
                code = qtcode[2:]
                mkt_code = 1 if prefix == "sh" else 0
                with lock:
                    results.append({
                        "f2": price, "f3": chg_pct, "f5": volume,
                        "f6": amount, "f8": turnover, "f10": vol_ratio,
                        "f12": code, "f13": mkt_code, "f14": name,
                        "f20": 0, "f21": float_cap * 1e8,
                    })
        except Exception as e:
            print(f"Tencent batch error: {e}")

    batches = [qtcodes[i:i + BATCH] for i in range(0, len(qtcodes), BATCH)]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
    futures = [executor.submit(fetch, b) for b in batches]
    try:
        for f in concurrent.futures.as_completed(futures, timeout=90):
            pass  # results collected via lock in fetch()
    except concurrent.futures.TimeoutError:
        print(f"Tencent scan timed out, using partial results ({len(results)} so far)")
    finally:
        executor.shutdown(wait=False)  # don't block on hung socket threads

    print(f"Tencent scan: {len(results)} stocks (chg>={chg_min}%) from {len(qtcodes)} codes")
    return results


# ── 涨停基因：20日内是否有涨停 ──────────────────────────────────
def check_limit_gene(code: str, mkt_code: int, days: int = 20) -> bool:
    try:
        r = _req.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{mkt_code}.{code}",
                "fields1": "f1,f2,f3,f4,f5",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": 101, "fqt": 0, "end": "20500101",
                "lmt": days + 3,
            },
            timeout=8,
        )
        klines = (r.json().get("data") or {}).get("klines") or []
        threshold = 19.5 if code.startswith("688") else 9.5
        for kline in klines[-days:]:
            parts = kline.split(",")
            if len(parts) >= 9 and safe_float(parts[8]) >= threshold:
                return True
        return False
    except Exception:
        return False


# ── 5日均线：当前价是否站上 MA5 ───────────────────────────────
def check_above_ma5(code: str, mkt_code: int) -> bool:
    """
    回测结论④：判断个股当前价是否站上 5 日均线（含当日）。
    5MA 下方的个股次日胜率显著偏低，过滤掉。
    获取失败时默认返回 True（不误拦截）。
    """
    try:
        r = _req.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{mkt_code}.{code}",
                "fields1": "f1,f2,f3,f4,f5",
                "fields2": "f51,f52,f53,f54,f55",
                "klt": 101, "fqt": 0, "end": "20500101", "lmt": 6,
            },
            timeout=6,
        )
        klines = (r.json().get("data") or {}).get("klines") or []
        closes = [safe_float(k.split(",")[2]) for k in klines[-5:]]  # parts[2]=收盘
        closes = [c for c in closes if c > 0]
        if len(closes) < 5:
            return True  # 数据不足，不拦截
        ma5 = sum(closes) / len(closes)
        return closes[-1] >= ma5
    except Exception:
        return True


# ── 完整行情快照 + 资金流（供"买入当日14:50"采集）────────────────
def _fetch_quote_snapshot(codes: list[str]) -> dict:
    """从腾讯API批量获取完整行情快照（含量比/换手率/振幅/市值等）。
    返回 {code: {features...}}。在买入时点调用即得到该时点的快照。"""
    snap: dict = {}
    qtcodes = [_qt_code(c) for c in codes]
    if not qtcodes:
        return snap
    try:
        r = _req.get(f"http://qt.gtimg.cn/q={','.join(qtcodes)}", timeout=8)
        for seg in r.text.strip().split(";"):
            m = re.search(r'v_(\w+)="([^"]+)"', seg)
            if not m:
                continue
            parts = m.group(2).split("~")
            if len(parts) < 50:
                continue
            code = m.group(1)[2:]
            snap[code] = {
                "name":          parts[1],
                "close":         safe_float(parts[3]),
                "prev_close":    safe_float(parts[4]),
                "open":          safe_float(parts[5]),
                "high":          safe_float(parts[33]),
                "low":           safe_float(parts[34]),
                "chg_pct":       safe_float(parts[32]),
                "volume":        safe_float(parts[36]),
                "amount":        safe_float(parts[37]),
                "turnover_rate": safe_float(parts[38]),
                "pe":            safe_float(parts[39]),
                "amplitude":     safe_float(parts[43]),
                "float_cap":     safe_float(parts[44]),
                "total_cap":     safe_float(parts[45]),
                "pb":            safe_float(parts[46]),
                "vol_ratio":     safe_float(parts[49]) if len(parts) > 49 else 0.0,
            }
    except Exception as e:
        print(f"snapshot fetch error: {e}")
    return snap


def _get_fund_flow(code: str) -> dict:
    """东方财富资金流向（主力/超大单/大单/小单净额）。"""
    mkt = "1" if code.startswith("6") else "0"
    try:
        r2 = _req.get(
            "http://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
            params={"lmt": "1", "klt": "1",
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                    "secid": f"{mkt}.{code}"},
            headers={"Referer": "http://quote.eastmoney.com/"}, timeout=6)
        rows = (r2.json().get("data") or {}).get("klines") or []
        if rows:
            p = rows[-1].split(",")
            return {"main_net": safe_float(p[1]), "huge_net": safe_float(p[2]),
                    "large_net": safe_float(p[3]), "small_net": safe_float(p[5])}
    except Exception:
        pass
    return {}


def _fetch_industry(code: str) -> str:
    """Fetch industry from EastMoney; fall back to first concept/region if industry is missing."""
    mkt = "1" if code.startswith("6") else "0"

    def clean_text(value) -> str:
        if not isinstance(value, str):
            return ""
        text = value.strip()
        return text if text and text not in ("-", "—") else ""

    def industry_from_payload(data: dict) -> str:
        industry = clean_text(data.get("f100")) or clean_text(data.get("f127"))
        if industry:
            return industry
        concepts = clean_text(data.get("f129") or data.get("f103"))
        if concepts:
            return f"概念:{concepts.split(',')[0]}"
        region = clean_text(data.get("f128") or data.get("f102"))
        if region:
            return f"地域:{region}"
        return ""

    try:
        for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
            r = _req.get(
                f"https://{host}/api/qt/stock/get",
                params={"secid": f"{mkt}.{code}", "fields": "f100,f127,f128,f129"},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                timeout=5,
            )
            industry = industry_from_payload(r.json().get("data") or {})
            if industry:
                return industry

        r = _req.get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params={
                "fltt": "2",
                "invt": "2",
                "fields": "f12,f14,f100,f102,f103",
                "secids": f"{mkt}.{code}",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            timeout=8,
        )
        rows = ((r.json().get("data") or {}).get("diff") or [])
        if rows:
            industry = industry_from_payload(rows[0])
            if industry:
                return industry
    except Exception:
        pass
    return "—"


def _sorted_intraday_times(intraday: dict) -> list[str]:
    """Return recorded intraday timestamps in chronological order."""
    if not isinstance(intraday, dict):
        return []
    times = []
    for key, val in intraday.items():
        if isinstance(val, dict) and re.match(r"^\d{1,2}:\d{2}$", str(key)):
            times.append(str(key))
    return sorted(times, key=lambda t: tuple(int(x) for x in t.split(":")))


def _intraday_price_points(intraday: dict, code: str) -> dict:
    """Return prices only for exact 09:30-10:00 market-time samples."""
    labels = ["09:30", "09:35", "09:40", "09:45", "09:50", "09:55", "10:00"]
    points = {}
    for label in labels:
        price = (intraday.get(label) or {}).get(code)
        if price is not None:
            points[label] = price
    return points


def _latest_intraday_prices(intraday: dict) -> dict:
    """Use the latest recorded intraday sample as the sell-price fallback."""
    times = _sorted_intraday_times(intraday)
    if not times:
        return {}
    return intraday.get(times[-1], {}) or {}


def _buy_snapshot_label(snapshot_time: str | None) -> str:
    """Describe the actual snapshot time without falsely claiming a 14:50 capture."""
    match = re.match(r"^(\d{2}):(\d{2})(?::\d{2})?$", str(snapshot_time or ""))
    if not match:
        return "unknown"
    hour, minute = (int(value) for value in match.groups())
    if _hm_value((14, 45)) <= _hm_value((hour, minute)) <= _hm_value((15, 0)):
        return "buy_1450"
    return f"buy_{hour:02d}{minute:02d}"


_MORNING_PRICE_LABELS = ("09:30", "09:35", "09:40", "09:45", "09:50", "09:55", "10:00")


def _qt_code(code: str) -> str:
    return f"{'sh' if str(code).startswith('6') else 'sz'}{code}"


def _minute_label(raw: str) -> str:
    raw = str(raw).strip()
    if re.match(r"^\d{4}$", raw):
        return f"{raw[:2]}:{raw[2:]}"
    return raw


def _previous_weekday(day: date) -> date:
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def _pending_buy_date_error(auto_buy: dict, today: str) -> str | None:
    buy_date = auto_buy.get("date")
    try:
        today_date = date.fromisoformat(today)
        buy_day = date.fromisoformat(buy_date)
    except Exception:
        return f"invalid pending buy date {buy_date!r}"

    expected = _previous_weekday(today_date)
    if buy_day != expected:
        return f"stale pending buy date {buy_date}; expected {expected.isoformat()}"
    return None


def _fetch_tencent_minute_points(codes: list[str]) -> dict:
    """Fetch 09:30-10:00 minute prices from Tencent's minute endpoint."""
    wanted = set(_MORNING_PRICE_LABELS)
    results: dict = {}
    for code in codes:
        qcode = _qt_code(code)
        points = {}
        try:
            r = _req.get(
                f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={qcode}",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=10,
            )
            payload = r.json()
            entry = ((payload.get("data") or {}).get(qcode) or {})
            rows = ((entry.get("data") or {}).get("data") or [])
            for row in rows:
                parts = str(row).split()
                if len(parts) < 2:
                    continue
                label = _minute_label(parts[0])
                if label in wanted:
                    price = safe_float(parts[1])
                    if price > 0:
                        points[label] = price
        except Exception:
            points = {}
        results[code] = points
    return results


def _intraday_from_minute_points(minute_points: dict) -> dict:
    intraday = {}
    for label in _MORNING_PRICE_LABELS:
        price_map = {
            code: points[label]
            for code, points in minute_points.items()
            if isinstance(points, dict) and points.get(label) is not None
        }
        if price_map:
            intraday[label] = price_map
    return intraday


def _recover_missing_sell_from_pending(today: str, auto_buy: dict, auto_trades: dict,
                                       auto_trades_sha: str | None, auto_buy_sha: str | None) -> dict:
    if not auto_buy or not auto_buy.get("positions"):
        return {"ok": False, "msg": "no pending positions"}

    buy_date = auto_buy.get("date")
    try:
        today_date = date.fromisoformat(today)
        buy_day = date.fromisoformat(buy_date)
    except Exception:
        return {"ok": False, "msg": f"invalid pending buy date {buy_date!r}"}

    if buy_day == today_date:
        return {"ok": False, "msg": "pending positions are for next trading day"}

    if buy_day != _previous_weekday(today_date):
        return {"ok": False, "msg": f"stale pending buy date {buy_date}"}

    positions = auto_buy.get("positions", [])
    codes = [p["code"] for p in positions if p.get("code")]
    minute_points = _fetch_tencent_minute_points(codes)
    intraday_patch = _intraday_from_minute_points(minute_points)
    sell_prices = {
        code: points.get("10:00")
        for code, points in minute_points.items()
        if isinstance(points, dict) and points.get("10:00")
    }
    if not sell_prices:
        return {"ok": False, "msg": "no recoverable 10:00 prices"}

    existing_trades = (auto_trades or {}).get("trades", [])
    existing_keys = {
        (t.get("buy_date"), t.get("sell_date"), t.get("code"))
        for t in existing_trades
    }

    cost_pct = 0.10
    settled = []
    for pos in positions:
        code = pos.get("code")
        sell_px = sell_prices.get(code)
        if not sell_px:
            continue
        key = (buy_date, today, code)
        if key in existing_keys:
            continue
        buy_px = safe_float(pos.get("buy_price"))
        if buy_px <= 0:
            continue
        settled.append({
            "code": code,
            "name": pos.get("name", code),
            "buy_date": buy_date,
            "sell_date": today,
            "buy_time": STRATEGY_BUY_TIME,
            "buy_price": round(buy_px, 3),
            "sell_price": round(sell_px, 3),
            "return_pct": round((sell_px / buy_px - 1) * 100 - cost_pct, 2),
            "industry": pos.get("industry", ""),
            "est_win_rate": pos.get("est_win_rate"),
            "criteria": pos.get("criteria", {}),
        })

    if not settled:
        return {"ok": False, "msg": "no new trades to recover"}

    intraday_path = f"sim_data/intraday/{buy_date}.json"
    intraday, intraday_sha = gh_read(intraday_path)
    intraday = intraday or {}
    for label, price_map in intraday_patch.items():
        merged = dict(intraday.get(label) or {})
        merged.update(price_map)
        intraday[label] = merged
    if not gh_write(intraday_path, intraday, intraday_sha, f"auto: recovered prices {buy_date}"):
        return {"ok": False, "msg": "failed to write recovered intraday prices"}

    updated_auto_trades = {"trades": (existing_trades + settled)[-90:]}
    if not gh_write("sim_data/auto_trades.json", updated_auto_trades, auto_trades_sha, f"auto: recover sell {today}"):
        return {"ok": False, "msg": "failed to write recovered auto trades"}

    if not gh_write("sim_data/auto_buy.json", {}, auto_buy_sha, f"auto: clear recovered {today}"):
        return {"ok": False, "msg": "failed to clear recovered auto buy"}

    return {
        "ok": True,
        "msg": f"recovered {len(settled)} trades",
        "auto_trades": updated_auto_trades,
        "intraday": {buy_date: intraday},
        "trades": settled,
    }


def _write_empty_trade_details(today: str, reason: str, now_cn: datetime, status: str = "no_trades") -> bool:
    path = f"sim_data/trade_details/{today}.json"
    existing, sha = gh_read(path)
    detail = {
        "date": today,
        "status": status,
        "reason": reason,
        "market_time": now_cn.strftime("%H:%M:%S"),
        "records": [],
    }
    return gh_write(path, detail, sha, f"trade_data: no trades {today}")


# ── 胜率预测（基于十年历史回测数据）──────────────────────────────
_IC_BASE = {(0.5, 1.0): 54, (1.0, 2.0): 43, (2.0, 99): 47, (0.0, 0.5): 38}
_WEEKDAY_BONUS = {0: 1, 1: -1, 2: 0, 3: 0, 4: 2}
_CONSEC_BONUS  = {0: 0, 1: 0, 2: 1, 3: 1}

def calc_market_win_rate(index_chg: float, weekday: int, consec_up: int) -> int:
    base = 38
    for (lo, hi), wr in _IC_BASE.items():
        if lo <= index_chg < hi:
            base = wr
            break
    bonus  = _WEEKDAY_BONUS.get(weekday % 7, 0)
    bonus += _CONSEC_BONUS.get(min(consec_up, 3), 0)
    return max(30, min(72, base + bonus))

def calc_stock_win_rate(market_win_rate: int, excess: float) -> int:
    if 2.0 <= excess <= 2.5:   delta = 3
    elif 2.5 < excess <= 3.0:  delta = 1
    elif 3.0 < excess <= 4.0:  delta = 0
    else:                      delta = -5
    return max(30, min(72, market_win_rate + delta))

def build_reasons(chg_pct: float, index_chg: float,
                  excess: float, market_win_rate: int) -> list:
    reasons = []
    if 3.0 <= chg_pct < 3.5:
        reasons.append(f"涨幅 {chg_pct:.1f}%，温和启动，追高风险低")
    elif chg_pct < 4.5:
        reasons.append(f"涨幅 {chg_pct:.1f}%，处于 3-5% 甜蜜区间，动能适中")
    else:
        reasons.append(f"涨幅 {chg_pct:.1f}%，偏高区间，次日需关注开盘方向")
    if 2.0 <= excess <= 2.5:
        reasons.append(f"超额大盘 {excess:.1f}x，独立行情且未过热（历史最优区间）")
    elif excess > 4.0:
        reasons.append(f"超额大盘 {excess:.1f}x，涨势较猛，注意次日开盘回调风险")
    else:
        reasons.append(f"超额大盘 {excess:.1f}x，个股有独立行情")
    reasons.append(f"大盘今日 +{index_chg:.2f}%，历史同类条件胜率约 {market_win_rate}%")
    return reasons


# ── 模拟交易：结算 + 记录 ─────────────────────────────────────
def _sim_run_settlement_and_record(scan_stocks: list) -> None:
    """在扫描完成后：结算昨日买入，更新胜率，记录今日 6/7+ 候选。"""
    today = _today_cn().isoformat()

    # 读取当前数据
    pending, pending_sha     = gh_read("sim_data/pending.json")
    trades_data, trades_sha  = gh_read("sim_data/trades.json")
    stats, stats_sha         = gh_read("sim_data/criteria_stats.json")

    if not stats:
        stats = _default_stats()
    if not trades_data:
        trades_data = {"trades": []}

    # 结算昨日持仓
    if pending and pending.get("date") and pending["date"] != today:
        # 专门查询持仓股票的当前价格（不能依赖扫描结果，持仓股今日不一定在3-5%区间）
        pending_codes = [p["code"] for p in pending.get("positions", [])]
        price_map = {}
        if pending_codes:
            try:
                qtcodes = [f"{'sh' if c.startswith('6') else 'sz'}{c}" for c in pending_codes]
                r = _req.get(f"http://qt.gtimg.cn/q={','.join(qtcodes)}", timeout=8)
                for seg in r.text.strip().split(";"):
                    m = re.search(r'v_(\w+)="([^"]+)"', seg)
                    if not m:
                        continue
                    parts = m.group(2).split("~")
                    if len(parts) >= 4:
                        code = m.group(1)[2:]  # 去掉 sh/sz 前缀
                        p = safe_float(parts[3])
                        if p > 0:
                            price_map[code] = p
            except Exception as e:
                print(f"sim price fetch error: {e}")
        settled = settle_trades(pending, price_map, sell_date=today)
        if settled:
            trades_data["trades"] = (trades_data.get("trades", []) + settled)[-90:]
            stats = update_criteria_stats(stats, settled)
            stats = auto_adjust_criteria(stats)
            gh_write("sim_data/trades.json",        trades_data, trades_sha, f"sim: trades {today}")
            gh_write("sim_data/criteria_stats.json", stats,       stats_sha,  f"sim: stats {today}")
        gh_write("sim_data/pending.json", {}, pending_sha, f"sim: clear pending {today}")

    # 记录今日 6/7+ 候选（基于活跃标准）
    active = set(stats.get("active_criteria", list(CRITERIA_KEYS)))
    n_active = len(active)
    min_score = max(n_active - 1, 3)  # 至少通过 n-1 条活跃标准

    sim_candidates = [
        {"code": s["code"], "name": s["name"], "price": s["price"],
         "criteria": s["criteria"], "score": s["score"]}
        for s in scan_stocks
        if s.get("score", 0) >= min_score
    ]

    # 只在 14:30-15:00 之间记录模拟买入（非尾盘时段扫描不算买入）
    now_cn = _now_cn()
    now_h, now_m = now_cn.hour, now_cn.minute
    in_buy_window = (14, 30) <= (now_h, now_m) <= (15, 0)
    if sim_candidates and in_buy_window:
        new_pending = {"date": today, "scan_time": now_cn.strftime("%H:%M:%S"),
                       "positions": sim_candidates}
        _, p_sha = gh_read("sim_data/pending.json")
        gh_write("sim_data/pending.json", new_pending, p_sha, f"sim: pending {today}")


# ── 主扫描逻辑 ─────────────────────────────────────────────────
def _run_scan_internal() -> dict:
    t0 = time.time()
    index_chg = get_index_chg()

    # 年线（MA250）保护：结构性熊市暂停操作（30年回测：年线下方操作收益为负）
    above_ma250 = get_index_above_ma250()

    # 今日市场胜率
    today_weekday = _now_cn().weekday()
    market_wr = calc_market_win_rate(index_chg, today_weekday, 1)
    # ── 大盘准入门槛（回测结论②：大盘当日涨幅 ≥ 0.5% 才操作）──
    market_blocked = False
    if not above_ma250:
        market_cond = f"大盘在年线（MA250）下方 ⚠️ 结构性熊市，暂停买入"
        market_wr = max(30, market_wr - 10)   # 年线下方胜率大幅下调
        market_blocked = True
    elif index_chg < 0.5:
        market_cond = f"大盘+{index_chg:.2f}%，未达 0.5% 门槛 ⚠️ 暂停买入（回测结论②）"
        market_blocked = True
    elif index_chg < 1.0:
        market_cond = f"大盘+{index_chg:.2f}%，0.5-1.0% 历史最优区间"
    elif index_chg < 2.0:
        market_cond = f"大盘+{index_chg:.2f}%，涨幅较强，历史胜率尚可"
    else:
        market_cond = f"大盘+{index_chg:.2f}%，涨幅过大，次日谨慎追高"

    # 大盘不达标（年线下方 或 涨幅 < 0.5%）：直接返回空结果，不扫描不买入
    if market_blocked:
        return {
            "stocks": [], "index_chg": round(index_chg, 2),
            "total_scanned": 0, "total_found": 0,
            "elapsed": round(time.time() - t0, 1),
            "scan_time": _now_cn().strftime("%H:%M:%S"),
            "active_criteria": [],
            "market_win_rate": market_wr,
            "market_condition": market_cond,
            "above_ma250": above_ma250,
        }

    # 读取活跃标准
    stats, _ = gh_read("sim_data/criteria_stats.json")
    active_criteria = set((stats or {}).get("active_criteria", list(CRITERIA_KEYS)))

    # Tencent 全量扫描
    codes = _gen_astock_qtcodes()
    raw = _tencent_batch_scan(codes, chg_min=2.5)

    if not raw:
        return {
            "error": "无法获取行情数据，请稍后重试",
            "stocks": [], "index_chg": index_chg,
            "total_found": 0, "elapsed": 0,
            "scan_time": _now_cn().strftime("%H:%M:%S"),
        }

    candidates = []
    for s in raw:
        code = str(s.get("f12") or "")
        name = str(s.get("f14") or "")
        if not code or "ST" in name.upper():
            continue

        price     = safe_float(s.get("f2"))
        chg_pct   = safe_float(s.get("f3"))
        turnover  = safe_float(s.get("f8"))
        vol_ratio = safe_float(s.get("f10"))
        float_cap = safe_float(s.get("f21")) / 1e8
        volume    = safe_float(s.get("f5"))
        amount    = safe_float(s.get("f6"))
        mkt_code  = int(s.get("f13") or 0)

        if price <= 0:
            continue

        # ① 涨幅 3-5%（核心条件，始终保留）
        if not (3.0 <= chg_pct <= 5.0):
            continue

        # ── 回测结论①：只做沪市（mkt_code==1）──
        if mkt_code != 1:
            continue

        # ── 回测结论③：超额大盘 2.0-2.5x（相对涨幅甜蜜区）──
        #    此处 index_chg ≥ 0.5（已过大盘门槛），excess 有意义
        excess = chg_pct / index_chg if index_chg > 0 else 0.0
        if not (2.0 <= excess <= 2.5):
            continue

        to_ok       = (5.0 <= turnover <= 10.0)  if "turnover"   in active_criteria else False
        vr_ok       = (vol_ratio > 1.0)           if "vol_ratio"  in active_criteria else False
        cap_ok      = (float_cap > 0 and 50.0 <= float_cap <= 200.0) if "cap" in active_criteria else False
        stronger_ok = (index_chg > 0 and chg_pct > index_chg)        if "stronger" in active_criteria else False

        if volume > 0 and amount > 0:
            vwap = amount / (volume * 100)
            vwap_ok = (price >= vwap) if "vwap" in active_criteria else False
        else:
            vwap, vwap_ok = price, False

        active_checks = [v for k, v in [
            ("turnover", to_ok), ("vol_ratio", vr_ok), ("cap", cap_ok),
            ("vwap", vwap_ok), ("stronger", stronger_ok),
        ] if k in active_criteria]

        if len(active_checks) > 0 and sum(active_checks) < 2:
            continue

        candidates.append({
            "code": code, "name": name, "price": price,
            "chg_pct": chg_pct, "turnover": turnover,
            "vol_ratio": vol_ratio, "float_cap": round(float_cap, 1),
            "vwap": round(vwap, 3), "mkt_code": mkt_code,
            "criteria": {
                "chg": True, "turnover": to_ok, "vol_ratio": vr_ok,
                "cap": cap_ok, "vwap": vwap_ok, "stronger": stronger_ok,
            },
            "score": 0,
        })

    # 计算评分（涨停基因已移除：回测显示胜率仅46.1%，低于基准2%，属于负效应标准）
    for s in candidates:
        s["score"] = sum(
            v for k, v in s["criteria"].items() if k in active_criteria
        )

    for s in candidates:
        if s["score"] == 0:
            s["score"] = sum(
                v for k, v in s["criteria"].items() if k in active_criteria
            )

    candidates.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))

    # ── 回测结论④：只保留站上 5 日均线（MA5）的个股 ──
    # 按排序从高分往下逐只查 MA5，凑满 Top5 即停（控制 API 调用次数）
    result_stocks = []
    for s in candidates:
        if len(result_stocks) >= 5:
            break
        if check_above_ma5(s["code"], s["mkt_code"]):
            result_stocks.append(s)

    # 批量查询行业板块（东方财富 API，只有5只，很快）
    for s in result_stocks:
        s["industry"] = _fetch_industry(s["code"])

    # 为每只股票注入胜率和推荐理由
    for s in result_stocks:
        excess = s["chg_pct"] / index_chg if index_chg > 0 else 0
        s["est_win_rate"] = calc_stock_win_rate(market_wr, excess)
        s["reasons"] = build_reasons(s["chg_pct"], index_chg, excess, market_wr)

    # 模拟交易：结算 + 记录（静默，不影响扫描结果）
    try:
        _sim_run_settlement_and_record(result_stocks)
    except Exception as e:
        print(f"sim settlement error (non-fatal): {e}")

    return {
        "stocks": result_stocks,
        "index_chg": round(index_chg, 2),
        "total_scanned": len(raw),
        "total_found": len(candidates),
        "elapsed": round(time.time() - t0, 1),
        "scan_time": _now_cn().strftime("%H:%M:%S"),
        "active_criteria": sorted(active_criteria),
        "market_win_rate": market_wr,
        "market_condition": market_cond,
        "above_ma250": above_ma250,
    }


# ── 同步缓存（在请求线程内扫描，避免 Render 后台线程 HTTP 限制）──
_cache: dict = {}
_cache_lock = threading.Lock()
_cache_ts: float = 0.0
CACHE_TTL = 300  # 5 分钟缓存有效期


# ── Flask 路由 ─────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/scan")
def api_scan():
    global _cache, _cache_ts
    now = time.time()
    with _cache_lock:
        if _cache and (now - _cache_ts) < CACHE_TTL:
            result = dict(_cache)
            result["cache_age"] = int(now - _cache_ts)
            return jsonify(result)

    # 缓存过期或无缓存：同步扫描（在本请求线程中执行）
    result = _run_scan_internal()
    with _cache_lock:
        _cache = result
        _cache_ts = time.time()
    return jsonify(result)


@app.route("/api/scan/force")
def api_scan_force():
    global _cache, _cache_ts
    with _cache_lock:
        _cache = {}
        _cache_ts = 0.0
    result = _run_scan_internal()
    with _cache_lock:
        _cache = result
        _cache_ts = time.time()
    return jsonify(result)


@app.route("/api/sim")
def api_sim():
    pending, _  = gh_read("sim_data/pending.json")
    trades_data, _ = gh_read("sim_data/trades.json")
    stats, _    = gh_read("sim_data/criteria_stats.json")
    trades = (trades_data or {}).get("trades", [])
    return jsonify({
        "pending":  pending or {},
        "trades":   trades[-20:],
        "stats":    stats or _default_stats(),
    })


@app.route("/api/auto-trades")
def api_auto_trades():
    """返回自动交易历史 + 当前持仓（供复盘页显示）"""
    auto_buy, _   = gh_read("sim_data/auto_buy.json")
    auto_trades, _ = gh_read("sim_data/auto_trades.json")
    trades = (auto_trades or {}).get("trades", [])
    return jsonify({
        "pending": auto_buy or {},
        "trades":  trades[-20:],
    })


@app.route("/api/index")
def api_index():
    chg = get_index_chg()
    return jsonify({"chg": round(chg, 2), "time": _now_cn().strftime("%H:%M:%S")})


# ── GitHub Actions 定时任务端点 ──────────────────────────────────────────────

@app.route("/api/actions/scan-and-buy", methods=["POST"])
def api_actions_scan_and_buy():
    """14:50 GitHub Actions 调用：扫描→取Top5→存 auto_buy.json"""
    now_cn = _now_cn()
    today = now_cn.date().isoformat()
    if not _action_in_window("scan_and_buy", now_cn):
        return _outside_window_response("scan_and_buy", now_cn)
    # 先清缓存，强制重新扫描
    global _cache, _cache_ts
    with _cache_lock:
        _cache = {}
        _cache_ts = 0.0
    result = _run_scan_internal()
    stocks = result.get("stocks", [])[:5]
    if not stocks:
        _, sha = gh_read("sim_data/auto_buy.json")
        gh_write("sim_data/auto_buy.json", {}, sha, f"auto: no buy {today}")
        snapshot_path = f"sim_data/signal_snapshots/{today}.json"
        _, snapshot_sha = gh_read(snapshot_path)
        signal_snapshot = build_signal_snapshot_doc(today, stocks, result)
        gh_write(snapshot_path, signal_snapshot, snapshot_sha, f"signal snapshot empty {today}")
        return jsonify({
            "ok": False,
            "msg": "无候选股（年线下方或无满足条件股票）",
            "date": today,
            "reason": signal_snapshot.get("no_buy_reason"),
            "index_chg": signal_snapshot.get("index_chg"),
            "market_condition": signal_snapshot.get("market_condition"),
            "total_scanned": signal_snapshot.get("total_scanned"),
            "total_found": signal_snapshot.get("total_found"),
        })

    auto_buy = {
        "date": today,
        "buy_time": STRATEGY_BUY_TIME,
        "market_win_rate": result.get("market_win_rate"),
        "index_chg": result.get("index_chg"),
        "positions": [
            {"code": s["code"], "name": s["name"], "buy_price": s["price"],
             "criteria": s["criteria"], "est_win_rate": s.get("est_win_rate"),
             "industry": s.get("industry", "—")}
            for s in stocks
        ],
    }
    _, sha = gh_read("sim_data/auto_buy.json")
    gh_write("sim_data/auto_buy.json", auto_buy, sha, f"auto: buy {today}")

    signal_snapshot = build_signal_snapshot_doc(today, stocks, result)
    snapshot_path = f"sim_data/signal_snapshots/{today}.json"
    _, snapshot_sha = gh_read(snapshot_path)
    gh_write(snapshot_path, signal_snapshot, snapshot_sha, f"signal snapshot {today}")

    # ── 买入当日 14:50 记录完整行情快照（含量比/换手/振幅/市值/资金流 +
    #    买入决策元信息），供卖出后分析使用；卖出日不再重新采集，避免用错时点数据 ──
    try:
        snap_codes = [s["code"] for s in stocks]
        quote_snap = _fetch_quote_snapshot(snap_codes)
        buy_snapshots: dict = {}
        for s in stocks:
            c = s["code"]
            qs = dict(quote_snap.get(c, {}))
            qs.update(_get_fund_flow(c))
            qs["buy_price"]    = s["price"]
            qs["est_win_rate"] = s.get("est_win_rate")
            qs["market_win_rate"] = result.get("market_win_rate")
            qs["index_chg"] = result.get("index_chg")
            qs["industry"]     = s.get("industry", "—")
            qs["criteria"]     = s.get("criteria", {})
            buy_snapshots[c] = qs
        snap_path = f"sim_data/buy_snapshots/{today}.json"
        _, snap_sha = gh_read(snap_path)
        gh_write(snap_path, {
            "date": today,
            "snapshot_time": now_cn.strftime("%H:%M:%S"),
            "snapshot_timezone": "Asia/Shanghai",
            "market_win_rate": result.get("market_win_rate"),
            "index_chg": result.get("index_chg"),
            "snapshots": buy_snapshots,
        }, snap_sha, f"buy snapshot {today}")
    except Exception as e:
        print(f"buy snapshot error (non-fatal): {e}")

    return jsonify({"ok": True, "date": today, "count": len(stocks),
                    "stocks": [s["name"] for s in stocks]})


@app.route("/api/actions/track-prices", methods=["POST"])
def api_actions_track_prices():
    """9:30-10:00 每5分钟 GitHub Actions 调用：记录持仓股票当前价格"""
    now_cn = _now_cn()
    if not _action_in_window("track_prices", now_cn):
        return _outside_window_response("track_prices", now_cn)

    auto_buy, _ = gh_read("sim_data/auto_buy.json")
    if not auto_buy or not auto_buy.get("positions"):
        return jsonify({"ok": False, "msg": "无持仓"})

    today = now_cn.date().isoformat()
    pending_date_error = _pending_buy_date_error(auto_buy, today)
    if pending_date_error:
        return jsonify({"ok": False, "msg": pending_date_error, "date": today})

    codes = [p["code"] for p in auto_buy["positions"]]
    qtcodes = [f"{'sh' if c.startswith('6') else 'sz'}{c}" for c in codes]
    price_map = {}
    try:
        r = _req.get(f"http://qt.gtimg.cn/q={','.join(qtcodes)}", timeout=8)
        for seg in r.text.strip().split(";"):
            m = re.search(r'v_(\w+)="([^"]+)"', seg)
            if not m: continue
            parts = m.group(2).split("~")
            if len(parts) >= 4:
                code = m.group(1)[2:]
                p = safe_float(parts[3])
                if p > 0:
                    price_map[code] = p
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

    today = auto_buy.get("date", today)
    now_time = now_cn.strftime("%H:%M")
    path = f"sim_data/intraday/{today}.json"
    intraday, sha = gh_read(path)
    if not intraday:
        intraday = {}
    intraday[now_time] = price_map
    gh_write(path, intraday, sha, f"auto: prices {today} {now_time}")
    return jsonify({"ok": True, "time": now_time, "prices": price_map})


@app.route("/api/actions/sell", methods=["POST"])
def api_actions_sell():
    """10:01 GitHub Actions 调用：用10:00价格结算，写入 auto_trades.json"""
    now_cn = _now_cn()
    if not _action_in_window("sell", now_cn):
        return _outside_window_response("sell", now_cn)

    auto_buy, buy_sha = gh_read("sim_data/auto_buy.json")
    if not auto_buy or not auto_buy.get("positions"):
        return jsonify({"ok": False, "msg": "无持仓"})

    today = now_cn.date().isoformat()
    buy_date = auto_buy.get("date", today)
    pending_date_error = _pending_buy_date_error(auto_buy, today)
    if pending_date_error:
        return jsonify({"ok": False, "msg": pending_date_error, "date": today})

    # 取10:00价格（先找 intraday，找不到就实时查）
    intraday, _ = gh_read(f"sim_data/intraday/{buy_date}.json")
    sell_prices = (intraday or {}).get("10:00", {}) or _latest_intraday_prices(intraday or {})

    if not sell_prices:
        # 实时查当前价格作为卖出价
        codes = [p["code"] for p in auto_buy["positions"]]
        qtcodes = [_qt_code(c) for c in codes]
        try:
            r = _req.get(f"http://qt.gtimg.cn/q={','.join(qtcodes)}", timeout=8)
            for seg in r.text.strip().split(";"):
                m = re.search(r'v_(\w+)="([^"]+)"', seg)
                if not m: continue
                parts = m.group(2).split("~")
                if len(parts) >= 4:
                    code = m.group(1)[2:]
                    p = safe_float(parts[3])
                    if p > 0:
                        sell_prices[code] = p
        except Exception:
            pass

    if not sell_prices:
        minute_points = _fetch_tencent_minute_points([p["code"] for p in auto_buy["positions"]])
        sell_prices = {
            code: points.get("10:00")
            for code, points in minute_points.items()
            if isinstance(points, dict) and points.get("10:00")
        }

    COST_PCT = 0.10
    settled = []
    for pos in auto_buy["positions"]:
        code = pos["code"]
        sell_px = sell_prices.get(code)
        if not sell_px:
            continue
        buy_px = float(pos["buy_price"])
        ret = round((sell_px / buy_px - 1) * 100 - COST_PCT, 2)
        settled.append({
            "code": code, "name": pos["name"],
            "buy_date": buy_date, "sell_date": today,
            "buy_time": STRATEGY_BUY_TIME,
            "buy_price": round(buy_px, 3), "sell_price": round(sell_px, 3),
            "return_pct": ret, "industry": pos.get("industry", "—"),
            "est_win_rate": pos.get("est_win_rate"),
            "criteria": pos.get("criteria", {}),
        })

    if not settled:
        return jsonify({"ok": False, "msg": "无法获取卖出价格"})

    # 追加到 auto_trades.json
    auto_trades, at_sha = gh_read("sim_data/auto_trades.json")
    if not auto_trades:
        auto_trades = {"trades": []}
    auto_trades["trades"] = (auto_trades["trades"] + settled)[-90:]
    if not gh_write("sim_data/auto_trades.json", auto_trades, at_sha, f"auto: sell {today}"):
        return jsonify({"ok": False, "msg": "failed to write auto_trades"})

    # 清空 auto_buy
    if not gh_write("sim_data/auto_buy.json", {}, buy_sha, f"auto: clear {today}"):
        return jsonify({"ok": False, "msg": "failed to clear auto_buy"})

    wins = sum(1 for t in settled if t["return_pct"] > 0)
    avg = round(sum(t["return_pct"] for t in settled) / len(settled), 2)
    return jsonify({"ok": True, "settled": len(settled), "wins": wins,
                    "avg_return": avg, "trades": settled})


@app.route("/api/actions/collect-trade-data", methods=["POST"])
def api_actions_collect_trade_data():
    """15:30 GitHub Actions 调用：收集今日交易的全量数据，存入 GitHub。"""
    now_cn = _now_cn()
    today = now_cn.date().isoformat()
    if not _action_in_window("collect_trade_data", now_cn):
        return _outside_window_response("collect_trade_data", now_cn)

    # 读取今日自动交易记录；如卖出任务漏跑，后面会尝试从上一交易日 pending 恢复。
    auto_trades, at_sha = gh_read("sim_data/auto_trades.json")
    if not auto_trades:
        auto_trades = {"trades": []}
    auto_buy, buy_sha = gh_read("sim_data/auto_buy.json")

    # 只处理今日成交
    today_trades = [t for t in auto_trades.get("trades", []) if t.get("sell_date") == today]
    if not today_trades:
        recovery = _recover_missing_sell_from_pending(today, auto_buy, auto_trades, at_sha, buy_sha)
        if recovery.get("ok"):
            auto_trades = recovery["auto_trades"]
            today_trades = [t for t in auto_trades.get("trades", []) if t.get("sell_date") == today]
            recovered_intraday = recovery.get("intraday", {})
        else:
            reason = recovery.get("msg", "no trades")
            empty_statuses = {
                "no pending positions": "no_trades",
                "pending positions are for next trading day": "pending_next_sell",
            }
            if reason in empty_statuses:
                status = empty_statuses[reason]
                if not _write_empty_trade_details(today, reason, now_cn, status=status):
                    return jsonify({"ok": False, "msg": "failed to write empty trade details"})
                return jsonify({
                    "ok": True,
                    "date": today,
                    "count": 0,
                    "status": status,
                    "reason": reason,
                    "stocks": [],
                })
            return jsonify({"ok": False, "msg": f"今日({today})无成交记录；{reason}"})
    else:
        recovered_intraday = {}

    # ── 读取"买入当日14:50"记录的行情快照（不再于卖出日重新采集，避免用错时点）──
    # today_trades 的 buy_date 可能不同，按 buy_date 缓存读取
    _snap_cache: dict = {}
    def _load_buy_snap(bdate: str) -> dict:
        if bdate not in _snap_cache:
            s, _ = gh_read(f"sim_data/buy_snapshots/{bdate}.json")
            _snap_cache[bdate] = s or {}
        return _snap_cache[bdate]

    # ── 读取卖出日早盘价格（intraday 文件按 buy_date 命名，结构 {time:{code:price}}）──
    _iday_cache: dict = {}
    def _load_intraday(bdate: str) -> dict:
        if bdate not in _iday_cache:
            if bdate in recovered_intraday:
                _iday_cache[bdate] = recovered_intraday[bdate]
            else:
                d, _ = gh_read(f"sim_data/intraday/{bdate}.json")
                _iday_cache[bdate] = d or {}
        return _iday_cache[bdate]

    # 读取 auto_buy（卖出后通常已清空；仅作元信息兜底）
    buy_map = {}
    if auto_buy:
        for p in auto_buy.get("positions", []):
            buy_map[p["code"]] = p

    # 组装完整记录（行情特征取自买入当日14:50快照，结果取自卖出成交）
    detail_records = []
    for t in today_trades:
        code = t["code"]
        bdate = t.get("buy_date", today)
        snap_doc = _load_buy_snap(bdate)
        snap = snap_doc.get("snapshots", {}).get(code, {})
        snapshot_time = snap_doc.get("snapshot_time")
        snapshot_timezone = snap_doc.get("snapshot_timezone")
        bp = buy_map.get(code, {})
        iday = _load_intraday(bdate)          # {time: {code: price}}
        ipoints = _intraday_price_points(iday, code)

        rec = {
            "trade_date":    today,
            "buy_date":      bdate,
            "sell_date":     today,
            "code":          code,
            "name":          snap.get("name") or t.get("name", ""),
            "industry":      snap.get("industry") or bp.get("industry", ""),
            "buy_price":     t.get("buy_price"),
            "buy_time":      STRATEGY_BUY_TIME,
            "sell_price":    t.get("sell_price"),
            "sell_time":     "10:01:00",
            "return_pct":    t.get("return_pct"),
            "snapshot_at":   _buy_snapshot_label(snapshot_time),
            "snapshot_time": snapshot_time,
            "snapshot_timezone": snapshot_timezone,
            "open":          snap.get("open"),
            "high":          snap.get("high"),
            "low":           snap.get("low"),
            "close":         snap.get("close"),
            "prev_close":    snap.get("prev_close"),
            "volume":        snap.get("volume"),
            "amount":        snap.get("amount"),
            "amplitude":     snap.get("amplitude"),
            "chg_pct":       snap.get("chg_pct"),
            "turnover_rate": snap.get("turnover_rate"),
            "vol_ratio":     snap.get("vol_ratio"),
            "float_cap":     snap.get("float_cap"),
            "total_cap":     snap.get("total_cap"),
            "pe":            snap.get("pe"),
            "pb":            snap.get("pb"),
            "main_net":      snap.get("main_net"),
            "huge_net":      snap.get("huge_net"),
            "large_net":     snap.get("large_net"),
            "small_net":     snap.get("small_net"),
            "price_next_open": ipoints.get("09:30") or t.get("sell_price"),
            "price_0930":    ipoints.get("09:30"),
            "price_0935":    ipoints.get("09:35"),
            "price_0940":    ipoints.get("09:40"),
            "price_0945":    ipoints.get("09:45"),
            "price_0950":    ipoints.get("09:50"),
            "price_0955":    ipoints.get("09:55"),
            "price_1000":    ipoints.get("10:00"),
            "est_win_rate":  snap.get("est_win_rate") if snap.get("est_win_rate") is not None else bp.get("est_win_rate"),
            "criteria":      snap.get("criteria") or bp.get("criteria", {}),
        }
        detail_records.append(rec)

    # 存入 GitHub
    path = f"sim_data/trade_details/{today}.json"
    existing, sha = gh_read(path)
    if not gh_write(path, {"date": today, "records": detail_records}, sha, f"trade_data: {today}"):
        return jsonify({"ok": False, "msg": "failed to write trade details"})

    return jsonify({"ok": True, "date": today, "count": len(detail_records),
                    "stocks": [r["name"] for r in detail_records]})


APP_VERSION = "v19-pending-next-sell-audit"


@app.route("/api/version")
def api_version():
    return jsonify({"version": APP_VERSION, "cache_ts": int(_cache_ts)})


@app.route("/api/debug/tencent")
def api_debug_tencent():
    """测试 Tencent API 在此服务器上是否可访问，返回前 5 只股票数据。"""
    import time as _t
    t0 = _t.time()
    test_codes = ["sh600519", "sh600036", "sz000001", "sz300750", "sh688981"]
    try:
        r = _req.get(f"http://qt.gtimg.cn/q={','.join(test_codes)}", timeout=10)
        results = []
        for seg in r.text.strip().split(";"):
            m = re.search(r'v_(\w+)="([^"]+)"', seg)
            if not m:
                continue
            parts = m.group(2).split("~")
            if len(parts) < 33:
                continue
            results.append({"code": m.group(1), "name": parts[1], "price": parts[3], "chg": parts[32]})
        return jsonify({
            "ok": True, "elapsed": round(_t.time() - t0, 2),
            "count": len(results), "stocks": results,
            "cache_ts": int(_cache_ts),
            "cache_scanned": _cache.get("total_scanned", 0) if _cache else 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "elapsed": round(_t.time() - t0, 2)})


@app.route("/api/debug/batch")
def api_debug_batch():
    """测试单个批量请求（50只股票），诊断批量扫描是否挂起。"""
    import time as _t
    t0 = _t.time()
    # 用真实存在的股票代码组成一批
    codes = [f"sh{600000+i}" for i in range(50)]
    try:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Referer": "https://finance.qq.com",
        })
        r = sess.get(f"http://qt.gtimg.cn/q={','.join(codes)}", timeout=(5, 8))
        count = sum(1 for seg in r.text.strip().split(";") if "~" in seg and "=" in seg)
        return jsonify({
            "ok": True, "elapsed": round(_t.time() - t0, 2),
            "batch_size": 50, "valid_segments": count,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "elapsed": round(_t.time() - t0, 2)})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5200))
    print(f"\n  一夜持股法选股助手 → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
