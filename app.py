"""一夜持股法选股助手 — 杨永兴七大硬标准扫描器"""

from __future__ import annotations

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


# ── EastMoney 全市场列表（主，Render海外IP可能被封） ──────────────
def _get_em_stocks(limit: int) -> list:
    try:
        r = _req.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "fid": "f3", "po": 1, "pz": limit, "pn": 1,
                "np": 1, "fltt": 2, "invt": 2,
                "fs": "m:1+t:2,m:0+t:6,m:0+t:80,m:1+t:23",
                "fields": "f2,f3,f5,f6,f8,f10,f12,f13,f14,f20,f21",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            },
            timeout=12,
        )
        result = r.json().get("data", {}).get("diff", []) or []
        if result:
            print(f"EastMoney OK: {len(result)} stocks")
        return result
    except Exception as e:
        print(f"EastMoney error: {e}")
        return []


# ── Sina Finance 备用（境外IP可访问） ──────────────────────────
def _get_sina_stocks() -> list:
    """Sina Market Center sorted by changepercent desc, enrich vol_ratio via Tencent"""
    sina_items = []
    for page in range(1, 8):
        try:
            r = _req.get(
                "http://vip.stock.finance.sina.com.cn/api/json.php/Market_Center.getHQNodeData",
                params={
                    "page": page, "num": 100,
                    "sort": "changepercent", "asc": 0,
                    "node": "hs_a", "_s_r_a": "page",
                },
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=10,
            )
            items = r.json()
            if not items:
                break
            for item in items:
                chg = safe_float(item.get("changepercent"))
                if chg >= 2.5:
                    symbol = item.get("symbol", "")
                    if symbol and len(symbol) > 2:
                        sina_items.append({
                            "symbol": symbol,
                            "name": item.get("name", ""),
                            "nmc": safe_float(item.get("nmc")),  # 流通市值，万元
                        })
            page_min = min((safe_float(x.get("changepercent")) for x in items), default=0)
            if page_min < 2.5:
                break
        except Exception as e:
            print(f"Sina page {page} error: {e}")
            break

    if not sina_items:
        return []

    print(f"Sina: {len(sina_items)} candidates ≥2.5% chg")

    # Enrich with Tencent for trading metrics (confirmed working from non-China IPs)
    symbol_map = {item["symbol"]: item for item in sina_items}
    all_symbols = list(symbol_map.keys())
    results = []

    for i in range(0, len(all_symbols), 80):
        batch = all_symbols[i:i + 80]
        try:
            r = _req.get(f"http://qt.gtimg.cn/q={','.join(batch)}", timeout=10)
            for seg in r.text.strip().split(";"):
                if "~" not in seg or "=" not in seg:
                    continue
                m = re.search(r'v_(\w+)="([^"]+)"', seg)
                if not m:
                    continue
                qtcode = m.group(1)
                if qtcode not in symbol_map:
                    continue
                parts = m.group(2).split("~")
                if len(parts) < 40:
                    continue
                price = safe_float(parts[3])
                if price <= 0:
                    continue
                nmc = symbol_map[qtcode]["nmc"]  # 万元 → *1e4 → 元
                results.append({
                    "f2":  price,
                    "f3":  safe_float(parts[32]),         # 涨跌幅%
                    "f5":  safe_float(parts[36]),         # 成交量(手)
                    "f6":  safe_float(parts[37]),         # 成交额(元)
                    "f8":  safe_float(parts[38]),         # 换手率%
                    "f10": safe_float(parts[49]) if len(parts) > 49 and safe_float(parts[49]) < 30 else 0,  # 量比
                    "f12": qtcode[2:],                    # 纯代码
                    "f13": 1 if qtcode[:2] == "sh" else 0,
                    "f14": symbol_map[qtcode]["name"],
                    "f20": 0,
                    "f21": nmc * 1e4,                    # 万元→元，同 EastMoney f21 单位
                })
        except Exception as e:
            print(f"Tencent batch {i//80} error: {e}")

    print(f"Sina+Tencent final: {len(results)} stocks")
    return results


def get_em_stocks(limit: int = 500) -> list:
    """Try EastMoney first; fall back to Sina + Tencent"""
    result = _get_em_stocks(limit)
    if result:
        return result
    return _get_sina_stocks()


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
def run_scan() -> dict:
    t0 = time.time()
    index_chg = get_index_chg()
    raw = get_em_stocks(500)

    if not raw:
        return {
            "error": "无法获取行情数据，EastMoney 和 Sina Finance API 均不可用",
            "stocks": [], "index_chg": 0, "total_found": 0, "elapsed": 0,
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
        float_cap = safe_float(s.get("f21")) / 1e8   # 元 → 亿
        volume    = safe_float(s.get("f5"))           # 手
        amount    = safe_float(s.get("f6"))           # 元
        mkt_code  = int(s.get("f13") or 0)

        if price <= 0:
            continue

        # ① 涨幅 3-5%（核心硬条件）
        if not (3.0 <= chg_pct <= 5.0):
            continue

        # ② 换手率 5-10%
        to_ok = 5.0 <= turnover <= 10.0

        # ③ 量比 > 1
        vr_ok = vol_ratio > 1.0

        # ④ 流通市值 50-200亿
        cap_ok = 50.0 <= float_cap <= 200.0

        # ⑥ 分时均线：股价 ≥ VWAP
        if volume > 0 and amount > 0:
            vwap = amount / (volume * 100)
            vwap_ok = price >= vwap
        else:
            vwap, vwap_ok = price, False

        # ⑦ 强于大盘
        stronger_ok = chg_pct > index_chg

        # 预筛：除涨幅外至少再满足 2 项
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

    # ⑤ 涨停基因（并行 HTTP，最多检查 40 只）
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


# ── Flask 路由 ─────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/scan")
def api_scan():
    return jsonify(run_scan())


@app.route("/api/index")
def api_index():
    chg = get_index_chg()
    return jsonify({"chg": round(chg, 2), "time": datetime.now().strftime("%H:%M:%S")})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5200))
    print(f"\n  一夜持股法选股助手 → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
