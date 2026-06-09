from __future__ import annotations

import os
import sys

import requests


DEFAULT_BASE_URL = "https://overnight-stock.onrender.com"
ACTION_PATHS = {
    "scan-and-buy": "/api/actions/scan-and-buy",
    "track-prices": "/api/actions/track-prices",
    "sell": "/api/actions/sell",
    "collect-trade-data": "/api/actions/collect-trade-data",
}


def build_url(action: str, base_url: str | None = None) -> str:
    if action not in ACTION_PATHS:
        valid = ", ".join(sorted(ACTION_PATHS))
        raise ValueError(f"unknown action {action!r}; expected one of: {valid}")
    base = (base_url or os.environ.get("OVERNIGHT_STOCK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}{ACTION_PATHS[action]}"


def call_action(action: str, base_url: str | None = None, post=requests.post) -> tuple[int, str]:
    try:
        response = post(
            build_url(action, base_url),
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        body = response.text
        response.raise_for_status()
        return 0, body
    except Exception as exc:
        body = getattr(locals().get("response", None), "text", "")
        detail = body or str(exc)
        return 1, detail


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) != 1:
        valid = ", ".join(sorted(ACTION_PATHS))
        print(f"usage: python scripts/render_cron_call.py <{valid}>", file=sys.stderr)
        return 2
    code, body = call_action(argv[0])
    if body:
        print(body)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
