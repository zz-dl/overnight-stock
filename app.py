"""一夜持股法选股助手 — 杨永兴七大硬标准扫描器"""

from __future__ import annotations

import concurrent.futures
import re
import socket
import threading
import time
from datetime import datetime, date
from math import isnan

import requests
from flask import Flask, jsonify, send_from_directory

from sim import (
    gh_read, gh_write, settle_trades,
    update_criteria_stats, auto_adjust_criteria,
    _default_stats, CRITERIA_KEYS,
)

socket.setdefaulttimeout(8)

app = Flask(__name__, static_folder="static")

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


# ── 上证指数涨跌幅 ─────────────────────────────────────────────
def get_index_chg() -> float:
    try:
        r = _req.get("http://qt.gtimg.cn/q=sh000001", timeout=5)
        parts = r.text.split("~")
        return safe_float(parts[32]) if len(parts) > 32 else 0.0
    except Exception:
        return 0.0


# ── A 股代码段 ────────────────────────────────────────────────
def _gen_astock_qtcodes() -> list[str]:
    """
    只扫活跃代码段，避免触发腾讯反爬。
    ~1800 只：沪主板核心段 + 深主板核心段 + 创业板核心 + 科创板核心。
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
    for n in range(1, 700):           # 深主板核心（000001-000699）
        pairs.append(f"sz{str(n).zfill(6)}")
    for n in range(2001, 2400):       # 深中小板（002001-002399）
        pairs.append(f"sz{str(n).zfill(6)}")
    for n in range(300000, 300600):   # 创业板核心（300000-300599）
        pairs.append(f"sz{n}")
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


# ── 模拟交易：结算 + 记录 ─────────────────────────────────────
def _sim_run_settlement_and_record(scan_stocks: list) -> None:
    """在扫描完成后：结算昨日买入，更新胜率，记录今日 6/7+ 候选。"""
    today = date.today().isoformat()

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

    if sim_candidates:
        new_pending = {"date": today, "scan_time": datetime.now().strftime("%H:%M:%S"),
                       "positions": sim_candidates}
        _, p_sha = gh_read("sim_data/pending.json")
        gh_write("sim_data/pending.json", new_pending, p_sha, f"sim: pending {today}")


# ── 主扫描逻辑 ─────────────────────────────────────────────────
def _run_scan_internal() -> dict:
    t0 = time.time()
    index_chg = get_index_chg()

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
            "scan_time": datetime.now().strftime("%H:%M:%S"),
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
    result_stocks = candidates[:30]

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
        "scan_time": datetime.now().strftime("%H:%M:%S"),
        "active_criteria": sorted(active_criteria),
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


@app.route("/api/index")
def api_index():
    chg = get_index_chg()
    return jsonify({"chg": round(chg, 2), "time": datetime.now().strftime("%H:%M:%S")})


APP_VERSION = "v5-sync-scan"


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
