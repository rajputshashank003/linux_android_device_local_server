"""Private, rotating MyForever activity and component-log reader."""
import json
import os
from pathlib import Path
import re
import threading
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
LOG_DIR = Path.home() / '.local' / 'state' / 'myforever'
AUDIT_FILE = LOG_DIR / 'activity.log'
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5
_LOCK = threading.RLock()
_ALLOWED = {
    'client_ip', 'method', 'route', 'status', 'duration_ms', 'search',
    'result_count', 'provider', 'model', 'selected_count', 'query_chars',
    'task', 'source_count', 'error_type', 'outcome', 'action', 'http_status',
    'message_count', 'online_count', 'file_count', 'item_count', 'bytes',
}
_SENSITIVE = re.compile(r'(?i)(?:sk-or-v1-[a-z0-9_-]{12,}|sk-[a-z0-9_-]{20,}|eyJ[a-z0-9_-]{40,}|bearer\s+[a-z0-9._~+/=-]{12,})')
_QUERY = re.compile(r"(?P<url>\b/(?:[^\s?\"']*))\?[^\s\"']*")
_KV_SECRET = re.compile(r"(?i)\b(authorization|api[_-]?key|secret|token|password)([\"\s]*[:=][\"\s]*)[^\s,}\"']+")
_AUTH = re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:basic|bearer)\s+[^\s,}\"']+")


def _private_dir():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass


def _clean(value, limit=300):
    text = ''.join(ch for ch in str(value) if ch in '\t ' or ord(ch) >= 32)
    if _SENSITIVE.search(text):
        return '[redacted sensitive-looking value]'
    return text[:limit]


def _clean_detail(key, value):
    if isinstance(value, (int, float, bool)):
        return value
    return _clean(value, 120 if key == 'search' else 300)


def record(event, client_ip=None, **details):
    """Append one allowlisted JSON event; logging failures never break requests."""
    payload = {
        'time': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'event': _clean(event, 80),
        'client_ip': _clean(client_ip, 80) if client_ip else '',
        'details': {key: _clean_detail(key, value)
                    for key, value in details.items() if key in _ALLOWED and value is not None},
    }
    line = json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n'
    try:
        _private_dir()
        with _LOCK:
            if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size + len(line.encode('utf-8')) > MAX_BYTES:
                oldest = LOG_DIR / f'{AUDIT_FILE.name}.{BACKUPS}'
                oldest.unlink(missing_ok=True)
                for n in range(BACKUPS - 1, 0, -1):
                    old, new = LOG_DIR / f'{AUDIT_FILE.name}.{n}', LOG_DIR / f'{AUDIT_FILE.name}.{n + 1}'
                    if old.exists(): old.replace(new)
                if AUDIT_FILE.exists(): AUDIT_FILE.replace(LOG_DIR / f'{AUDIT_FILE.name}.1')
            with AUDIT_FILE.open('a', encoding='utf-8') as stream:
                stream.write(line)
            try: os.chmod(AUDIT_FILE, 0o600)
            except OSError: pass
    except OSError:
        pass


def _audit_entries(limit):
    entries = []
    for n in range(BACKUPS, 0, -1):
        path = LOG_DIR / f'{AUDIT_FILE.name}.{n}'
        try:
            if path.is_file() and not path.is_symlink():
                entries.extend(_parse_lines(path))
        except OSError:
            continue
    try:
        if AUDIT_FILE.is_file() and not AUDIT_FILE.is_symlink():
            entries.extend(_parse_lines(AUDIT_FILE))
    except OSError:
        pass
    return entries[-limit:]


def _parse_lines(path):
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()[-MAX_BYTES:]
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict) and isinstance(item.get('event'), str):
                result.append(item)
        except (ValueError, TypeError):
            continue
    return result


def _component_tail(path, source, count=60):
    try:
        if not path.is_file() or path.is_symlink():
            return []
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 256 * 1024))
            lines = stream.read().decode('utf-8', 'replace').splitlines()[-count:]
    except OSError:
        return []
    result = []
    for line in lines:
        safe = _QUERY.sub(r'\g<url>?[query omitted]', line)
        safe = _AUTH.sub('Authorization: [redacted]', safe)
        safe = _KV_SECRET.sub(r'\1\2[redacted]', safe)
        safe = _SENSITIVE.sub('[redacted sensitive-looking value]', safe)
        result.append({'source': source, 'line': safe[:1000]})
    return result


def logs(limit=300):
    """Return recent application events and tails of known service logs only."""
    limit = max(1, min(int(limit), 500))
    _private_dir()
    components = [
        ('supervisor', ROOT / 'myserver.log'),
        ('cloudflared', ROOT / 'cloudflared.log'),
        ('local-ai', LOG_DIR / 'local-ai.log'),
    ]
    config = Path.home() / '.config' / 'myforever'
    components.extend((('direct-proxy', config / 'caddy.log'), ('dns-updater', config / 'godaddy-dns.log')))
    return {
        'events': _audit_entries(limit),
        'components': [{'source': source, 'lines': _component_tail(path, source)}
                       for source, path in components],
        'limits': {'events': limit, 'linesPerComponent': 60, 'rotationMiB': MAX_BYTES // (1024 * 1024), 'rotatedFiles': BACKUPS},
        'privacy': 'Search terms and client IPs are recorded. Prompts, chat messages, document contents, passwords and API credentials are not logged. The server uses one shared password, so client IP is the only available session clue.',
    }
