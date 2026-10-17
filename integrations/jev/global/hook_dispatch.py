"""MCP lifecycle adapter. Private launch context stays in the server environment."""
import importlib.util
import json
from pathlib import Path

_hook = None


def dispatch(event, metadata):
    if not isinstance(event, dict) or not isinstance(metadata, dict):
        raise ValueError("Invalid lifecycle callback")
    if not event.get("session_id") or event["session_id"] != metadata.get("threadId"):
        raise ValueError("Lifecycle callback does not belong to this thread")
    if len(json.dumps(event)) > 2_000_000:
        raise ValueError("Lifecycle callback is too large")
    global _hook
    if _hook is None:
        path = Path(__file__).with_name("codex_hook.py")
        spec = importlib.util.spec_from_file_location("jev_global_hook", path)
        _hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_hook)
    name = event.get("hook_event_name")
    if name not in _hook.HANDLERS:
        raise ValueError("Unsupported lifecycle callback")
    try:
        return _hook.HANDLERS[name](event) or {}
    except Exception:
        # Keep the command hook's explicit fail-closed decision, without exception text.
        return _hook.deny("Jev tool guard failed closed; check the lifecycle hook.") if name == "PreToolUse" else {}
