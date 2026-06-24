from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


DEFAULT_BASE_URL = "https://overnight-stock.onrender.com"
MARKET_TZ = ZoneInfo("Asia/Shanghai")
TRACK_PRICE_LABELS = ("09:30", "09:35", "09:40", "09:45", "09:50", "09:55", "10:00")
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


def _market_now() -> datetime:
    return datetime.now(MARKET_TZ)


def _as_market_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MARKET_TZ)
    return value.astimezone(MARKET_TZ)


def _parse_hm(value: str) -> tuple[int, int, int | None]:
    parts = str(value).split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid market time {value!r}; expected HH:MM or HH:MM:SS")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and (second is None or 0 <= second <= 59)):
        raise ValueError(f"invalid market time {value!r}; expected HH:MM or HH:MM:SS")
    return hour, minute, second


def _time_on_market_day(now_cn: datetime, label: str, end_of_minute: bool = False) -> datetime:
    hour, minute, second = _parse_hm(label)
    if second is None:
        second = 59 if end_of_minute else 0
        microsecond = 999999 if end_of_minute else 0
    else:
        microsecond = 0
    return now_cn.replace(hour=hour, minute=minute, second=second, microsecond=microsecond)


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


def wait_and_call(
    action: str,
    target_time: str,
    deadline_time: str,
    base_url: str | None = None,
    now=_market_now,
    sleep=time.sleep,
    call=call_action,
) -> tuple[int, str]:
    now_cn = _as_market_time(now())
    target = _time_on_market_day(now_cn, target_time)
    deadline = _time_on_market_day(now_cn, deadline_time, end_of_minute=True)
    if now_cn > deadline:
        return 1, json.dumps({
            "ok": False,
            "msg": f"missed {action} window",
            "market_time": now_cn.strftime("%H:%M:%S"),
            "deadline": deadline_time,
        })
    if now_cn < target:
        sleep(max(0, int((target - now_cn).total_seconds())))
    return call(action, base_url=base_url)


def track_price_session(
    labels: tuple[str, ...] = TRACK_PRICE_LABELS,
    base_url: str | None = None,
    now=_market_now,
    sleep=time.sleep,
    call=call_action,
) -> tuple[int, str]:
    ran = 0
    for label in labels:
        now_cn = _as_market_time(now())
        deadline = _time_on_market_day(now_cn, label, end_of_minute=True)
        if now_cn > deadline:
            continue
        code, body = wait_and_call(
            "track-prices",
            label,
            label,
            base_url=base_url,
            now=now,
            sleep=sleep,
            call=call,
        )
        if code != 0:
            return code, body
        ran += 1
    if ran == 0:
        now_cn = _as_market_time(now())
        return 1, json.dumps({
            "ok": False,
            "msg": "missed track-prices window",
            "market_time": now_cn.strftime("%H:%M:%S"),
        })
    return 0, "track session complete"


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) == 1 and argv[0] in ACTION_PATHS:
        code, body = call_action(argv[0])
    elif len(argv) in (3, 4) and argv[0] == "wait-and-call":
        action = argv[1]
        target = argv[2]
        deadline = argv[3] if len(argv) == 4 else target
        code, body = wait_and_call(action, target, deadline)
    elif len(argv) == 1 and argv[0] == "track-session":
        code, body = track_price_session()
    else:
        valid = ", ".join(sorted(ACTION_PATHS))
        print(
            "usage: python scripts/render_cron_call.py "
            f"<{valid}> | wait-and-call <action> <HH:MM> [deadline HH:MM] | track-session",
            file=sys.stderr,
        )
        return 2
    if body:
        print(body)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
