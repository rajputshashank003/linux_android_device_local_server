"""Bounded, owner-scoped operational events for Agentic Chat."""
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
import re
import time

_MAX_EVENTS_PER_OWNER = 512
_MAX_OWNERS = 128
_events = defaultdict(lambda: deque(maxlen=_MAX_EVENTS_PER_OWNER))
_lock = Lock()
_last_id = 0
_SECRET = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*|((?:api[_-]?key|token|password|authorization)\s*[:=]\s*)[^\s,;]+')


def _safe_text(value, limit=240):
    text = _SECRET.sub(lambda m: (m.group(1) or m.group(2) or '') + '[REDACTED]', str(value))
    return text.replace('\r', ' ').replace('\n', ' ')[:limit]


def record(owner, message, state="info", *, request_id="", conversation_id="", source="api",
           stage="lifecycle", level=None, metadata=None, overview=False):
    """Record a non-content operational event. Prompts and model output are never accepted."""
    try:
        from app_db import validate_owner
        owner = validate_owner(owner)
    except (ImportError, ValueError):
        return None
    if state not in ("info", "working", "success", "error", "warning", "debug"):
        state = "info"
    level = (level or {"working": "INFO", "success": "INFO", "error": "ERROR", "warning": "WARN", "debug": "DEBUG"}.get(state, "INFO")).upper()
    if level not in ("DEBUG", "INFO", "WARN", "ERROR"):
        level = "INFO"
    safe_metadata = {}
    if isinstance(metadata, dict):
        for key, value in list(metadata.items())[:12]:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", str(key)) or re.search(r"(?i)(key|token|password|authorization|secret)", str(key)):
                continue
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                safe_metadata[key] = value
            elif isinstance(value, str):
                safe_metadata[key] = _safe_text(value, 100)
    global _last_id
    with _lock:
        if owner not in _events and len(_events) >= _MAX_OWNERS:
            _events.pop(next(iter(_events)), None)
        now = time.time()
        _last_id = max(_last_id + 1, int(now * 1_000_000))
        event = {
            "id": _last_id, "time": now,
            "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
            "requestId": _safe_text(request_id, 64) if request_id else "",
            "conversationId": _safe_text(conversation_id, 64) if conversation_id else "",
            "level": level, "source": _safe_text(source, 32), "stage": _safe_text(stage, 48),
            "status": state, "state": state, "overview": bool(overview),
            "message": _safe_text(message), "metadata": safe_metadata,
        }
        _events[owner].append(event)
        return dict(event)


def list_events(owner, after=0, limit=512, request_id="", conversation_id=""):
    from app_db import validate_owner
    owner = validate_owner(owner)
    try:
        after = max(0, int(after))
    except (TypeError, ValueError):
        after = 0
    try:
        limit = max(1, min(_MAX_EVENTS_PER_OWNER, int(limit)))
    except (TypeError, ValueError):
        limit = 512
    with _lock:
        all_events = tuple(_events.get(owner, ()))
        events = [dict(event) for event in all_events if event["id"] > after
                  and (not request_id or event["requestId"] == request_id)
                  and (not conversation_id or event["conversationId"] == conversation_id)]
        cursor = max((event["id"] for event in all_events), default=after)
    return {"events": events[-limit:], "cursor": cursor, "state": "available", "retained": len(all_events)}
