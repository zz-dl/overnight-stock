from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "https://overnight-stock.onrender.com"
ACTION_PATHS = {
    "scan-and-buy": "/api/actions/scan-and-buy",
    "track-prices": "/api/actions/track-prices",
    "sell": "/api/actions/sell",
    "collect-trade-data": "/api/actions/collect-trade-data",
}


def _response_text(response) -> str:
    if hasattr(response, "text"):
        return response.text
    data = response.read()
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _raise_for_status(response) -> None:
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
        return
    status = getattr(response, "status", getattr(response, "status_code", 200))
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")


def _post(url: str, headers: dict[str, str], timeout: int):
    request = urllib.request.Request(url, method="POST", headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


def is_fatal_action_response(body: str) -> bool:
    try:
        payload = json.loads(body)
    except Exception:
        return False
    if payload.get("ok") is not False:
        return False
    msg = str(payload.get("msg", ""))
    return msg.startswith("outside ") and " window" in msg


def build_url(action: str, base_url: str | None = None) -> str:
    if action not in ACTION_PATHS:
        valid = ", ".join(sorted(ACTION_PATHS))
        raise ValueError(f"unknown action {action!r}; expected one of: {valid}")
    base = (base_url or os.environ.get("OVERNIGHT_STOCK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}{ACTION_PATHS[action]}"


def call_action(action: str, base_url: str | None = None, post=_post) -> tuple[int, str]:
    try:
        response = post(
            build_url(action, base_url),
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        body = _response_text(response)
        _raise_for_status(response)
        if is_fatal_action_response(body):
            return 1, body
        return 0, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body or str(exc)
        return 1, detail
    except Exception as exc:
        body = ""
        if "response" in locals():
            try:
                body = _response_text(response)
            except Exception:
                body = ""
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
