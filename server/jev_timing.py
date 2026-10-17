"""Bounded timing telemetry; never accept URLs, request bodies or exception text."""

import json
import math
import time

PHASES = {"helper_start", "input_read", "request_start", "connecting", "waiting_headers",
          "reading_body", "parsing_response", "complete", "python_open"}
METRICS = {"startup_ms", "helper_elapsed_ms", "request_at_ms", "connect_started_at_ms",
           "connected_at_ms", "headers_at_ms", "body_done_at_ms", "parse_done_at_ms",
           "elapsed_ms", "budget_ms"}
MAX_MS = 86_400_000


def safe_timing(value):
    if (not isinstance(value, dict) or not isinstance(value.get("phase"), str)
            or value["phase"] not in PHASES):
        return {}
    result = {"phase": value["phase"]}
    if value.get("transport") in ("node", "python"):
        result["transport"] = value["transport"]
    if type(value.get("observed")) is bool:
        result["observed"] = value["observed"]
    for name in METRICS:
        number = value.get(name)
        if type(number) in (int, float) and math.isfinite(number) and 0 <= number <= MAX_MS:
            result[name] = round(number, 1)
    return result


def helper_timing(stderr, started, timeout):
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    latest = {}
    if isinstance(stderr, str) and len(stderr) <= 32_768:
        for line in stderr.splitlines()[-32:]:
            try:
                value = json.loads(line) if len(line) <= 2048 else None
            except ValueError:
                continue
            if isinstance(value, dict) and set(value) == {"jev_timing"}:
                candidate = safe_timing(value["jev_timing"])
                if candidate:
                    latest = candidate
    return safe_timing({**latest, "phase": latest.get("phase", "helper_start"),
                        "transport": "node", "observed": bool(latest),
                        "elapsed_ms": (time.monotonic() - started) * 1000,
                        "budget_ms": timeout * 1000})


def timing_record(value):
    safe = safe_timing(value)
    return {"jev_timing": safe} if safe else {}
