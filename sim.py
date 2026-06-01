"""Simulation trade logic for 一夜持股法."""

import base64, json, os, re
from math import isnan

import requests

CRITERIA_KEYS = ("chg", "turnover", "vol_ratio", "cap", "vwap", "stronger")
_WINDOW = 30
_DEACTIVATE_THRESHOLD = 0.40
_REACTIVATE_THRESHOLD = 0.50
_MIN_ACTIVE = 3

_GH_REPO   = "zz-dl/overnight-stock"
_GH_BRANCH = "master"


# ── GitHub 存储 ──────────────────────────────────────────────────────────────

def _gh_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _gh_hdrs() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    t = _gh_token()
    if t:
        h["Authorization"] = f"token {t}"
    return h


def gh_read(path: str):
    """Read JSON from GitHub API. Returns (data, sha) or (None, None)."""
    url = f"https://api.github.com/repos/{_GH_REPO}/contents/{path}?ref={_GH_BRANCH}"
    try:
        r = requests.get(url, headers=_gh_hdrs(), timeout=10)
        if r.status_code == 200:
            j = r.json()
            return json.loads(base64.b64decode(j["content"]).decode()), j.get("sha")
    except Exception:
        pass
    return None, None


def gh_write(path: str, data, sha: str = None, message: str = "sim: update") -> bool:
    """Write JSON to GitHub. Returns True on success."""
    t = _gh_token()
    if not t:
        return False
    content = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode()
    ).decode()
    url = f"https://api.github.com/repos/{_GH_REPO}/contents/{path}"
    body: dict = {"message": message, "content": content, "branch": _GH_BRANCH}
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, json=body, headers=_gh_hdrs(), timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False


# ── 结算逻辑 ─────────────────────────────────────────────────────────────────

def settle_trades(pending: dict, prices: dict, sell_date: str) -> list:
    """
    Settle pending positions using current prices.
    Returns list of completed trade dicts.
    Skips any position whose code is not in prices.
    """
    trades = []
    for pos in pending.get("positions", []):
        code = pos.get("code", "")
        sell_price = prices.get(code)
        if sell_price is None:
            continue
        buy_price = float(pos.get("price", 0))
        if buy_price <= 0:
            continue
        return_pct = round((sell_price / buy_price - 1) * 100, 2)
        trades.append({
            "code":       code,
            "name":       pos.get("name", code),
            "buy_price":  buy_price,
            "sell_price": sell_price,
            "buy_date":   pending.get("date", ""),
            "sell_date":  sell_date,
            "return_pct": return_pct,
            "criteria":   pos.get("criteria", {}),
            "score":      pos.get("score", 0),
        })
    return trades


# ── 胜率统计 ─────────────────────────────────────────────────────────────────

def _default_stats() -> dict:
    return {
        "active_criteria": list(CRITERIA_KEYS),
        "stats": {
            k: {"wins": 0, "losses": 0, "win_rate": 0.0, "active": True}
            for k in CRITERIA_KEYS
        },
    }


def update_criteria_stats(stats: dict, new_trades: list) -> dict:
    """
    Update rolling win/loss counts (capped at _WINDOW) and recompute win_rate.
    Only counts trades where the criterion was passed (True).
    """
    if not stats:
        stats = _default_stats()

    for trade in new_trades:
        won = trade.get("return_pct", 0) > 0
        criteria = trade.get("criteria", {})
        for k in CRITERIA_KEYS:
            if not criteria.get(k):
                continue  # criterion wasn't passed — don't count
            s = stats["stats"][k]
            if won:
                s["wins"] += 1
            else:
                s["losses"] += 1
            # Rolling window: trim oldest if over limit
            total = s["wins"] + s["losses"]
            if total > _WINDOW:
                excess = total - _WINDOW
                # Remove from the longer side first
                if s["wins"] >= s["losses"]:
                    s["wins"] = max(0, s["wins"] - excess)
                else:
                    s["losses"] = max(0, s["losses"] - excess)
            total = s["wins"] + s["losses"]
            s["win_rate"] = round(s["wins"] / total, 3) if total > 0 else 0.0

    return stats


# ── 自动启停标准 ─────────────────────────────────────────────────────────────

def auto_adjust_criteria(stats: dict) -> dict:
    """
    Deactivate criteria with win_rate < _DEACTIVATE_THRESHOLD.
    Reactivate criteria with win_rate >= _REACTIVATE_THRESHOLD.
    Always keep at least _MIN_ACTIVE criteria (highest win_rate ones).
    """
    if not stats:
        stats = _default_stats()

    s = stats["stats"]

    # Reactivate first
    for k in CRITERIA_KEYS:
        if not s[k]["active"] and s[k]["win_rate"] >= _REACTIVATE_THRESHOLD:
            s[k]["active"] = True

    # Deactivate: only if enough active remain
    currently_active = [k for k in CRITERIA_KEYS if s[k]["active"]]
    # Sort by win_rate desc to know which to protect
    sorted_by_rate = sorted(currently_active, key=lambda k: s[k]["win_rate"], reverse=True)

    for k in CRITERIA_KEYS:
        if not s[k]["active"]:
            continue
        if s[k]["win_rate"] >= _DEACTIVATE_THRESHOLD:
            continue
        # Would deactivating still leave _MIN_ACTIVE active?
        remaining_active = [c for c in CRITERIA_KEYS
                            if s[c]["active"] and c != k]
        if len(remaining_active) >= _MIN_ACTIVE:
            s[k]["active"] = False

    stats["active_criteria"] = [k for k in CRITERIA_KEYS if s[k]["active"]]
    return stats
