"""一夜持股法选股助手 — 杨永兴七大硬标准扫描器"""

from __future__ import annotations

import concurrent.futures
import re
import threading
import time
from datetime import datetime
from math import isnan

import requests
from flask import Flask, jsonify, send_from_directory

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
    pairs: list[str] = []
    for n in range(600000, 604000):   # 沪主板（活跃段）
        pairs.append(f"sh{n}")
    for n in range(688000, 689000):   # 科创板
        pairs.append(f"sh{n}")
    for n in range(1, 3000):          # 深主板+中小板
        pairs.append(f"sz{str(n).zfill(6)}")
    for n in range(300000, 302000):   # 创业板
        pairs.append(f"sz{n}")
    return pairs


# ── 腾讯行情批量扫描（ThreadPoolExecutor，真正并发）──────────────
def _tencent_batch_scan(qtcodes: list[str], chg_min: float = 2.5) -> list[dict]:
    BATCH = 80
    results: list[dict] = []
    lock = threading.Lock()

    def fetch(batch: list[str]) -> None:
        try:
            r = _req.get(f"http://qt.gtimg.cn/q={','.join(batch)}", timeout=10)
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
        except Exception:
            pass

    batches = [qtcodes[i:i + BATCH] for i in range(0, len(qtcodes), BATCH)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as ex:
        ex.map(fetch, batches)

    print(f"Tencent scan: {len(results)} stocks with chg>={chg_min}% from {len(qtcodes)} codes")
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


# ── 主扫描逻辑 ─────────────────────────────────────────────────
def _run_scan_internal() -> dict:
    t0 = time.time()
    index_chg = get_index_chg()

    # Tencent 全量扫描（海外 IP 可用，EastMoney 海外只给 100 条无法覆盖 3-5% 区间）
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

        # ① 涨幅 3-5%
        if not (3.0 <= chg_pct <= 5.0):
            continue

        # ② 换手率 5-10%
        to_ok = 5.0 <= turnover <= 10.0
        # ③ 量比 > 1
        vr_ok = vol_ratio > 1.0
        # ④ 流通市值 50-200亿
        cap_ok = float_cap > 0 and (50.0 <= float_cap <= 200.0)
        # ⑥ 分时均线：股价 ≥ VWAP
        if volume > 0 and amount > 0:
            vwap = amount / (volume * 100)
            vwap_ok = price >= vwap
        else:
            vwap, vwap_ok = price, False
        # ⑦ 强于大盘
        stronger_ok = index_chg > 0 and chg_pct > index_chg

        if sum([to_ok, vr_ok, cap_ok, vwap_ok, stronger_ok]) < 2:
            continue

        candidates.append({
            "code": code, "name": name, "price": price,
            "chg_pct": chg_pct, "turnover": turnover,
            "vol_ratio": vol_ratio, "float_cap": round(float_cap, 1),
            "vwap": round(vwap, 3), "mkt_code": mkt_code,
            "criteria": {
                "chg": True, "turnover": to_ok, "vol_ratio": vr_ok,
                "cap": cap_ok, "limit_gene": False,
                "vwap": vwap_ok, "stronger": stronger_ok,
            },
            "score": 0,
        })

    # ⑤ 涨停基因（并行，最多 40 只）
    top = candidates[:40]

    def _check(stock: dict) -> None:
        stock["criteria"]["limit_gene"] = check_limit_gene(
            stock["code"], stock["mkt_code"]
        )
        stock["score"] = sum(stock["criteria"].values())

    threads = [threading.Thread(target=_check, args=(s,), daemon=True) for s in top]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)

    for s in candidates:
        if s["score"] == 0:
            s["score"] = sum(s["criteria"].values())

    candidates.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))

    return {
        "stocks": candidates[:30],
        "index_chg": round(index_chg, 2),
        "total_scanned": len(raw),
        "total_found": len(candidates),
        "elapsed": round(time.time() - t0, 1),
        "scan_time": datetime.now().strftime("%H:%M:%S"),
    }


# ── 后台缓存 ───────────────────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
_cache_ts: float = 0.0
_scan_running = False


def _do_scan_bg() -> None:
    global _cache, _cache_ts, _scan_running
    if _scan_running:
        return
    _scan_running = True
    try:
        result = _run_scan_internal()
        with _cache_lock:
            _cache = result
            _cache_ts = time.time()
    except Exception as e:
        print(f"Background scan error: {e}")
    finally:
        _scan_running = False


def _start_bg_scan() -> None:
    threading.Thread(target=_do_scan_bg, daemon=True).start()


# ── Flask 路由 ─────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/scan")
def api_scan():
    now = time.time()
    cache_age = now - _cache_ts

    if cache_age > 300 and not _scan_running:
        _start_bg_scan()

    if _cache:
        result = dict(_cache)
        result["refreshing"] = _scan_running
        result["cache_age"] = int(cache_age)
        return jsonify(result)

    # 无缓存：后台扫描中，让客户端轮询
    return jsonify({
        "scanning": True,
        "stocks": [],
        "total_scanned": 0,
        "total_found": 0,
        "message": "正在扫描全市场A股，约30秒后自动刷新…",
    })


@app.route("/api/scan/force")
def api_scan_force():
    global _cache, _cache_ts
    with _cache_lock:
        _cache = {}
        _cache_ts = 0.0
    _start_bg_scan()
    return jsonify({
        "scanning": True,
        "stocks": [],
        "total_scanned": 0,
        "total_found": 0,
        "message": "已触发重新扫描，约30秒后点击刷新结果…",
    })


@app.route("/api/index")
def api_index():
    chg = get_index_chg()
    return jsonify({"chg": round(chg, 2), "time": datetime.now().strftime("%H:%M:%S")})


# 启动时立即开始后台扫描（减少冷启动等待）
_start_bg_scan()

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5200))
    print(f"\n  一夜持股法选股助手 → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
