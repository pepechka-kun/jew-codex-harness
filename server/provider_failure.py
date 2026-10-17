"""Transport failure classes without storing upstream response bodies."""

import json
import re


def model_rejection_reason(status, body):
    """Recognize explicit model refusals; return constants, never provider text."""
    if status not in (400, 404, 422):
        return None
    try:
        parsed = json.loads(body) if body else None
    except (ValueError, UnicodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    error = error if isinstance(error, dict) else {}
    if error.get("code") in ("model_not_found", "model_not_supported", "unsupported_model"):
        return "model_unavailable"
    # OmniRoute preserves the upstream detail separately from its generic error.
    details = parsed.get("upstream_details")
    messages = [error.get("message"), parsed.get("detail")]
    if isinstance(details, dict):
        messages.append(details.get("detail"))
    for message in messages:
        if (isinstance(message, str) and len(message) <= 2048 and re.fullmatch(
                r"The '[^'\r\n]{1,256}' model is not supported when using Codex with a ChatGPT account\.",
                message.strip())):
            return "model_not_supported_for_account"
    return None


def safe_model_rejection(reason):
    """Public replacement for a recognized refusal, with no freeform body."""
    message = ("The configured provider does not support the selected model for this account."
               if reason == "model_not_supported_for_account" else
               "The selected model is unavailable from the configured provider.")
    return json.dumps({"error": {"message": message, "type": "invalid_request_error",
                                 "code": reason}}).encode("utf-8")


def classify_failure(status, quota_hit=False, body=None, attempt=None):
    attempt = attempt or {}
    phase = attempt.get("timeout_phase")
    code = None
    try:
        parsed = json.loads(body) if body else None
        error = parsed.get("error") if isinstance(parsed, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
    except (ValueError, UnicodeError, TypeError):
        pass
    model_reason = model_rejection_reason(status, body)
    if attempt.get("completion") == "client_disconnected":
        category, seconds, retry = "client_disconnected", 0, False
    elif phase in ("total", "max_attempts"):
        category, seconds, retry = "budget_exhausted", 0, False
    elif model_reason and not quota_hit:
        category = "model_account_unavailable" if model_reason == "model_not_supported_for_account" else "model_unavailable"
        seconds, retry = 300, True
    elif status in (400, 413, 422) and not quota_hit:
        category, seconds, retry = "bad_request", 0, False
    elif status in (401, 403):
        category, seconds, retry = "auth", 900, True
    elif status == 404:
        category, seconds, retry = "model_unavailable", 300, True
    elif status == 429 and code in ("rate_limit_exceeded", "rate_limit", "too_many_requests"):
        category, seconds, retry = "rate_limit", 30, True
    elif quota_hit or status in (402, 429):
        category, seconds, retry = "quota", 300, True
    elif phase or status in (408, 504):
        category, seconds, retry = "timeout", 30, True
    elif (status == 200 or str(attempt.get("completion", "")).startswith("invalid")):
        category, seconds, retry = "invalid_response", 30, True
    elif status >= 500 or attempt.get("transport_error"):
        category, seconds, retry = "unavailable", 30, True
    else:
        category, seconds, retry = "bad_request", 0, False
    return {"class": category, "cooldown_seconds": seconds, "retryable": retry,
            **({"reason": model_reason} if model_reason and category in ("model_unavailable", "model_account_unavailable") else {})}
