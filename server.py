#!/usr/bin/env python3
"""Password-protected browser file server for a local network."""
import base64
import binascii
import email.utils
import getpass
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit, parse_qs

import server_logs

USERNAME = "admin"
ROOT = Path(__file__).resolve().parent / "shared"
UI_FILE = Path(__file__).resolve().parent / "ui.html"
HOSTING_UI_FILE = Path(__file__).resolve().parent / "hosting.html"
MAIN_NAVIGATION_FILE = Path(__file__).resolve().parent / "main_navigation.html"
MAX_UPLOAD = 512 * 1024 * 1024
CHUNK_SIZE = 128 * 1024
MUTATION_LOCK = threading.RLock()
CLIPBOARD_LOCK = threading.RLock()
CLIPBOARD_TEXT = ""
CLIPBOARD_UPDATED = None
MAX_CLIPBOARD_BYTES = 32768


def server_password_from_environment(environ=None):
    """Return an explicitly configured password, optionally loaded from a private file."""
    environ = os.environ if environ is None else environ
    password = environ.get("TERMUX_SERVER_PASSWORD")
    if password:
        return password
    filename = environ.get("TERMUX_SERVER_PASSWORD_FILE")
    if not filename:
        return None
    path = Path(filename).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Configured server password file is unavailable.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Configured server password path must be a regular file.")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("Configured server password file must be private (mode 600).")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise RuntimeError("Configured server password file exceeds the supported size.")
        try:
            password = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Configured server password file must contain UTF-8 text.") from exc
        if not password:
            raise RuntimeError("Configured server password file is empty.")
        return password
    finally:
        os.close(fd)


PREVIEW_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".markdown": "text/plain; charset=utf-8", ".json": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8", ".py": "text/plain; charset=utf-8",
    ".js": "text/plain; charset=utf-8", ".html": "text/plain; charset=utf-8",
    ".htm": "text/plain; charset=utf-8", ".css": "text/plain; charset=utf-8",
    ".xml": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8", ".yml": "text/plain; charset=utf-8",
}


def safe_path(raw, allow_root=True):
    """Resolve a decoded relative path below ROOT; reject traversal and symlinks."""
    value = raw or ""
    if "\\" in value or "\x00" in value or value.startswith("/"):
        return None
    parts = value.split("/") if value else []
    if any(not part or part in (".", "..") or any(ord(ch) < 32 for ch in part) for part in parts):
        return None
    if ROOT.is_symlink():
        return None
    root = ROOT.resolve()
    target = root.joinpath(*parts)
    try:
        target.resolve(strict=False).relative_to(root)
    except (ValueError, OSError):
        return None
    probe = root
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            return None
    return target if allow_root or parts else None


def safe_leaf(name):
    return bool(name and name not in (".", "..") and "/" not in name and "\\" not in name
                and "\x00" not in name and not any(ord(ch) < 32 for ch in name))


def query_value(query, key, default=""):
    return parse_qs(query, keep_blank_values=True).get(key, [default])[0]


def metadata(path):
    st = path.stat()
    return {"size": st.st_size if path.is_file() else None,
            "modified": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            "folder": path.is_dir()}


SERVER_STARTED = time.monotonic()


class FileHandler(BaseHTTPRequestHandler):
    server_version = "TermuxFileServer/1.2"
    sys_version = ""

    def authenticate(self):
        try:
            scheme, encoded = self.headers.get("Authorization", "").split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError
            user, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            user, password = "", ""
        if hmac.compare_digest(user.encode("utf-8"), USERNAME.encode("utf-8")) and hmac.compare_digest(password.encode("utf-8"), self.server.password.encode("utf-8")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Termux file server", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=()):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers may cancel a long request or navigate away before the
            # response arrives. Treat that as a normal disconnect, not a server error.
            self.close_connection = True

    def json_reply(self, status, obj):
        self.reply(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _proxy_hosted_route(self):
        try:
            import hosting
            if not hosting.is_hosted_path(self.path): return False
            hosting.proxy_request(self)
            return True
        except Exception as exc:
            self.reply(502, ("Hosted application proxy failed: " + type(exc).__name__).encode("utf-8"))
            return True

    def do_GET(self):
        if self._proxy_hosted_route(): return
        if not self.authenticate():
            return
        route = urlsplit(self.path)
        q = route.query
        if (route.path in ("/", "/agentic", "/host", "/host/new", "/host/existing")
                or re.fullmatch(r"/deployed/[A-Za-z0-9_-]{1,64}", route.path)):
            page_file = HOSTING_UI_FILE if (route.path in ("/host", "/host/new", "/host/existing") or re.fullmatch(r"/deployed/[A-Za-z0-9_-]{1,64}", route.path)) else UI_FILE
            try:
                page = page_file.read_bytes()
                marker = b"<!-- MAIN_NAVIGATION_MENU -->"
                if marker in page:
                    page = page.replace(marker, MAIN_NAVIGATION_FILE.read_bytes())
            except OSError:
                self.reply(500, b"Hosting page or shared navigation is missing" if page_file == HOSTING_UI_FILE else b"UI file or shared navigation is missing")
                return
            self.reply(200, page, "text/html; charset=utf-8", (("Cache-Control", "no-store"), ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; img-src 'self' blob: data:; media-src 'self' blob:; frame-src 'self' blob:; object-src 'none'; base-uri 'none'"),))
        elif route.path == "/api/capabilities":
            self.json_reply(200, {"management": True, "preview": True, "range": True, "zip": True, "clipboard": True, "chat": True, "agent": True, "localAI": True, "directHosting": True, "logs": True, "pdfjs": True, "health": True, "agenticChat": True, "rag": True, "databaseExplorer": True, "memoryOptimization": True, "hosting": True})
        elif route.path == "/api/hosting/status":
            try:
                import hosting
                self.json_reply(200, hosting.runtime_status())
            except Exception as exc:self.json_reply(503,{"available":False,"error":"Hosting diagnostics unavailable: "+type(exc).__name__})
        elif route.path == "/api/hosting/apps":
            try:
                import hosting
                self.json_reply(200,{"apps":hosting.list_applications()})
            except Exception as exc:self.json_reply(503,{"error":"Applications could not be listed: "+type(exc).__name__})
        elif route.path.startswith("/api/hosting/apps/"):
            try:
                import hosting
                parts=route.path.split("/")
                app_id=parts[4] if len(parts)>4 else ""
                result=hosting.app_logs(app_id) if len(parts)>5 and parts[5]=="logs" else hosting.get_application(app_id)
                self.json_reply(200 if result else 404,result or {"error":"Application not found"})
            except Exception as exc:self.json_reply(503,{"error":"Application details unavailable: "+type(exc).__name__})
        elif route.path == "/api/database/overview":
            try:
                import db_explorer
                self.json_reply(200, db_explorer.overview())
            except Exception as exc:
                self.json_reply(503, {"state":"unavailable", "error":"Database inspection unavailable: "+type(exc).__name__})
        elif route.path == "/api/database/table":
            try:
                import db_explorer
                self.json_reply(200, db_explorer.table(query_value(q,"name",""),
                    page=query_value(q,"page","1"), limit=query_value(q,"limit","25"),
                    search=query_value(q,"search",""), sort=query_value(q,"sort",""),
                    direction=query_value(q,"direction","asc")))
            except ValueError as exc:
                self.json_reply(400, {"error":str(exc)})
            except Exception as exc:
                self.json_reply(503, {"error":"Database table could not be read: "+type(exc).__name__})
        elif route.path == "/api/health/live":
            self.json_reply(200, {"state":"alive","uptimeSeconds":round(time.monotonic()-SERVER_STARTED)})
        elif route.path == "/api/health/ready":
            try:
                import app_db
                database=app_db.health()
                self.json_reply(200 if database.get("state")=="healthy" else 503, {"state":"ready" if database.get("state")=="healthy" else "degraded","database":database.get("state")})
            except Exception as exc:self.json_reply(503,{"state":"degraded","database":"unavailable","error":type(exc).__name__})
        elif route.path == "/api/health":
            try:
                import health
                self.json_reply(200, health.snapshot(query_value(q, "probe", "0") == "1"))
            except Exception as exc:
                self.json_reply(503, {"overall":"degraded","error":type(exc).__name__})
        elif route.path == "/api/agentic/activity":
            try:
                import agent_activity
                self.json_reply(200, agent_activity.list_events(
                    query_value(q, "owner"), query_value(q, "after", "0"),
                    query_value(q, "limit", "512"), query_value(q, "requestId", ""),
                    query_value(q, "conversationId", "")))
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Agent activity is unavailable: "+type(exc).__name__})
        elif route.path == "/api/agentic/metrics/memory":
            try:
                import health
                self.json_reply(200, health.memory_snapshot())
            except Exception as exc:self.json_reply(503,{"error":"Memory metrics unavailable: "+type(exc).__name__})
        elif route.path == "/api/conversations":
            try:
                import app_db
                self.json_reply(200, {"conversations":app_db.list_conversations(query_value(q,"owner"))})
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Conversation database unavailable: "+type(exc).__name__})
        elif route.path.startswith("/api/conversations/"):
            try:
                import app_db
                cid=route.path.removeprefix("/api/conversations/")
                result=app_db.get_conversation(query_value(q,"owner"),cid)
                self.json_reply(200 if result else 404,result or {"error":"Conversation not found"})
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Conversation database unavailable: "+type(exc).__name__})
        elif route.path == "/api/rag/status":
            try:
                import rag
                self.json_reply(200,rag.status())
            except Exception as exc:self.json_reply(503,{"state":"unavailable","error":type(exc).__name__})
        elif route.path == "/api/agent/status":
            from agent import status as agent_status
            self.json_reply(200, agent_status())
        elif route.path == "/api/agent/files":
            from agent import scan_files
            self.json_reply(200, {"items": scan_files()})
        elif route.path == "/api/local-ai/status":
            from local_ai import status
            self.json_reply(200, status(query_value(q, "model", "") or None))
        elif route.path == "/api/local-ai/logs":
            from local_ai import logs
            self.json_reply(200, logs())
        elif route.path == "/api/direct-hosting/status":
            from direct_host import status
            self.json_reply(200, status())
        elif route.path == "/api/direct-hosting/logs":
            from direct_host import logs
            self.json_reply(200, logs())
        elif route.path == "/api/logs":
            try:
                limit = int(query_value(q, "limit", "300"))
            except ValueError:
                limit = 300
            self.json_reply(200, server_logs.logs(limit))
        elif route.path.startswith("/assets/pdfjs/"):
            self.serve_pdfjs_asset(route.path)
        elif route.path == "/ws/chat":
            from chat import handle_websocket
            handle_websocket(self)
        elif route.path == "/api/clipboard":
            with CLIPBOARD_LOCK:
                self.json_reply(200, {"text": CLIPBOARD_TEXT, "updated": CLIPBOARD_UPDATED})
        elif route.path == "/api/list":
            self.list_directory(query_value(q, "path"))
        elif route.path == "/api/search":
            self.search_files(query_value(q, "q"))
        elif route.path in ("/preview", "/download"):
            self.stream_file(query_value(q, "path"), inline=route.path == "/preview")
        elif route.path == "/zip":
            self.zip_directory(query_value(q, "path"))
        else:
            self.reply(404, b"Not found")

    def serve_pdfjs_asset(self, route_path):
        """Serve only the pinned PDF.js runtime assets, never arbitrary local files."""
        prefix = "/assets/pdfjs/6.4.299/"
        if not route_path.startswith(prefix):
            self.reply(404, b"Not found")
            return
        relative = route_path[len(prefix):]
        parts = relative.split("/")
        if (not parts or any(part in ("", ".", "..") or "\\" in part for part in parts)
                or len(parts) != 2):
            self.reply(404, b"Not found")
            return
        section, name = parts
        allowed = ((section == "build" and name in ("pdf.mjs", "pdf.worker.mjs"))
                   or (section == "cmaps" and name.endswith(".bcmap"))
                   or (section == "wasm" and name.endswith((".wasm", ".mjs", ".js")))
                   or (section == "standard_fonts" and name.endswith((".pfb", ".ttf", ".otf"))))
        if not allowed:
            self.reply(404, b"Not found")
            return
        base = Path(__file__).resolve().parent / "assets" / "pdfjs" / "6.4.299"
        target = base / section / name
        try:
            if target.is_symlink() or not target.resolve(strict=True).is_relative_to(base.resolve()):
                self.reply(404, b"Not found")
                return
            size = target.stat().st_size
        except (OSError, ValueError):
            self.reply(404, b"Not found")
            return
        content_type = ("text/javascript; charset=utf-8" if name.endswith((".mjs", ".js"))
                        else "application/wasm" if name.endswith(".wasm")
                        else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=604800")
        self.end_headers()
        if self.command != "HEAD":
            try:
                with target.open("rb") as asset:
                    while True:
                        chunk = asset.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (OSError, BrokenPipeError, ConnectionResetError):
                return

    def list_directory(self, raw):
        folder = safe_path(raw)
        if folder is None or not folder.is_dir():
            self.json_reply(400, {"error": "Folder not found"})
            return
        rows = []
        try:
            for item in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
                if item.is_symlink():
                    continue
                rel = item.relative_to(ROOT).as_posix()
                row = {"name": item.name, "path": rel, **metadata(item)}
                rows.append(row)
        except OSError:
            self.json_reply(500, {"error": "Could not read folder"})
            return
        self.json_reply(200, {"path": folder.relative_to(ROOT).as_posix() if folder != ROOT else "", "items": rows})

    def search_files(self, query):
        term = query.strip().casefold()
        found = []
        if term:
            for base, dirs, files in os.walk(ROOT, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                for name in dirs + files:
                    path = Path(base) / name
                    if term in name.casefold() and not path.is_symlink():
                        try:
                            found.append({"name": name, "path": path.relative_to(ROOT).as_posix(), **metadata(path)})
                        except OSError:
                            continue
                        if len(found) >= 500:
                            break
                if len(found) >= 500:
                    break
        server_logs.record("file.search", self.client_address[0], search=query.strip(), result_count=len(found))
        self.json_reply(200, {"items": found})

    def stream_file(self, raw, inline):
        target = safe_path(raw, allow_root=False)
        if target is None or not target.is_file() or target.is_symlink():
            self.reply(404, b"File not found")
            return
        extension = target.suffix.lower()
        mime = PREVIEW_TYPES.get(extension, "application/octet-stream")
        if inline and extension not in PREVIEW_TYPES:
            self.json_reply(415, {"error": "No browser preview for this file type"})
            return
        if extension in (".html", ".htm", ".xml"):
            mime = "text/plain; charset=utf-8"
        try:
            stat = target.stat()
            size = stat.st_size
            etag = f'"{stat.st_ino:x}-{size:x}-{stat.st_mtime_ns:x}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            start, end, status = 0, size - 1, 200
            range_header = self.headers.get("Range")
            if_range = self.headers.get("If-Range")
            use_range = bool(range_header)
            if use_range and if_range:
                use_range = if_range == etag
                if not use_range and not if_range.startswith(('"', 'W/')):
                    try:
                        validator_date = email.utils.parsedate_to_datetime(if_range).timestamp()
                        use_range = stat.st_mtime <= validator_date
                    except (TypeError, ValueError, OverflowError):
                        use_range = False
            if use_range:
                parsed = self.parse_range(range_header, size)
                if parsed is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                if parsed != "ignore":
                    start, end = parsed
                    status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", email.utils.formatdate(stat.st_mtime, usegmt=True))
            self.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(target.name))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, no-cache")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            # The CSP sandbox blocks some browsers' built-in PDF viewers in iframes.
            # Keep the sandbox on text/image/audio/video previews, but let the native
            # PDF viewer apply its own isolation for application/pdf responses.
            if inline and extension != ".pdf":
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; style-src 'unsafe-inline'; media-src 'self' blob:; img-src 'self' blob: data:")
            self.end_headers()
            if self.command == "HEAD":
                return
            with target.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (OSError, BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def parse_range(value, size):
        if not value.startswith("bytes=") or "," in value:
            return "ignore"
        spec = value[6:].strip()
        if "-" not in spec:
            return "ignore"
        first, last = spec.split("-", 1)
        try:
            if not first:
                suffix = int(last)
                if suffix <= 0:
                    return None
                return max(0, size - suffix), size - 1
            start = int(first)
            end = int(last) if last else size - 1
        except ValueError:
            return "ignore"
        if start < 0 or start >= size or end < start:
            return None
        return start, min(end, size - 1)

    def zip_directory(self, raw):
        folder = safe_path(raw)
        if folder is None or not folder.is_dir():
            self.reply(400, b"Folder not found")
            return
        archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        try:
            prefix = folder.name or "shared"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for base, dirs, files in os.walk(folder, followlinks=False):
                    dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                    for filename in files:
                        path = Path(base) / filename
                        if path.is_symlink() or not path.is_file():
                            continue
                        arcname = Path(prefix) / path.relative_to(folder) if folder != ROOT else Path(path.relative_to(ROOT))
                        zf.write(path, arcname.as_posix())
            archive.seek(0, os.SEEK_END)
            size = archive.tell()
            archive.seek(0)
            download_name = (folder.name or "shared") + ".zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(download_name))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = archive.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, zipfile.BadZipFile, BrokenPipeError):
            return
        finally:
            archive.close()

    def do_HEAD(self):
        if self._proxy_hosted_route(): return
        if not self.authenticate():
            return
        route = urlsplit(self.path)
        if route.path in ("/preview", "/download"):
            self.stream_file(query_value(route.query, "path"), inline=route.path == "/preview")
        elif route.path == "/zip":
            self.zip_directory(query_value(route.query, "path"))
        else:
            self.reply(404, b"Not found")

    def do_PUT(self):
        if self._proxy_hosted_route(): return
        if not self.authenticate():
            return
        route = urlsplit(self.path)
        if route.path == "/api/clipboard":
            data = self.read_json()
            if data is None or not isinstance(data.get("text"), str):
                self.json_reply(400, {"error": "Clipboard text must be valid JSON text"})
                return
            text = data["text"]
            if len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
                self.json_reply(413, {"error": "Clipboard text is limited to 32 KiB"})
                return
            global CLIPBOARD_TEXT, CLIPBOARD_UPDATED
            with CLIPBOARD_LOCK:
                CLIPBOARD_TEXT = text
                CLIPBOARD_UPDATED = datetime.now().astimezone().isoformat(timespec="seconds")
                updated = CLIPBOARD_UPDATED
            self.json_reply(200, {"text": text, "updated": updated})
        elif route.path == "/api/agent/tags":
            data = self.read_json()
            if data is None or not isinstance(data.get("path"), str) or not isinstance(data.get("tags"), list):
                self.json_reply(400, {"error": "Provide a file path and a list of tags"})
                return
            try:
                from agent import set_tags
                tags = set_tags(data["path"], data["tags"])
            except ValueError as exc:
                self.json_reply(400, {"error": str(exc)})
                return
            self.json_reply(200, {"path": data["path"], "tags": tags})
        elif route.path == "/api/mkdir":
            self.make_directory(route.query)
        elif route.path == "/api/upload":
            self.upload(route.query)
        else:
            self.reply(404, b"Not found")

    def read_json(self, max_bytes=65536):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 <= length <= max_bytes:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (UnicodeError, json.JSONDecodeError):
            return None

    def do_DELETE(self):
        if self._proxy_hosted_route(): return
        self.reply(405, b"Method not allowed")

    def do_POST(self):
        if self._proxy_hosted_route(): return
        if not self.authenticate():
            return
        route = urlsplit(self.path)
        data = self.read_json(12 * 1024 * 1024 if route.path == "/api/agent/ask" else 65536)
        if data is None:
            self.json_reply(400, {"error": "Invalid JSON request"})
            return
        if route.path == "/api/hosting/apps":
            try:
                import hosting
                result=hosting.create_application(data)
                self.json_reply(202,result)
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except RuntimeError as exc:self.json_reply(409,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Deployment could not start: "+type(exc).__name__})
            return
        if route.path == "/api/hosting/source":
            try:
                import hosting
                result=hosting.create_node_source(data)
                self.json_reply(202,result)
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except RuntimeError as exc:self.json_reply(409,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Node.js source deployment could not start: "+type(exc).__name__})
            return
        if route.path.startswith("/api/hosting/apps/"):
            parts=route.path.split("/"); app_id=parts[4] if len(parts)>4 else ""
            action=data.get("action")
            try:
                import hosting, app_db
                if action == "start": result=hosting.start_application(app_id)
                elif action == "stop": result=hosting.stop_application(app_id)
                elif action == "restart": result=hosting.restart_application(app_id)
                elif action == "redeploy": result=hosting.redeploy_application(app_id)
                elif action == "publish":
                    app=app_db.update_hosted_app(app_id,hosting.OWNER,public=bool(data.get("public")))
                    result=hosting.public_app(app) if app else None
                elif action == "delete":
                    if data.get("confirm") != app_id:
                        self.json_reply(400,{"error":"Confirm deletion by sending the application ID."}); return
                    result={"deleted":hosting.delete_application(app_id)}
                else:
                    self.json_reply(400,{"error":"Unsupported application action."}); return
                self.json_reply(202 if result and action in ("start","restart","redeploy") else (200 if result else 404),result or {"error":"Application not found"})
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except RuntimeError as exc:self.json_reply(409,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Application action failed: "+type(exc).__name__})
            return
        if route.path == "/api/conversations":
            try:
                import app_db
                result=app_db.create_conversation(data.get("owner"),data.get("title","New chat"),data.get("model",""))
                self.json_reply(201,result)
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Conversation database unavailable: "+type(exc).__name__})
            return
        if route.path == "/api/conversations/message":
            try:
                import app_db
                result=app_db.add_message(data.get("owner"),data.get("id"),data.get("role"),data.get("content"),data.get("model",""),data.get("metadata"))
                self.json_reply(201,result) if result else self.json_reply(404,{"error":"Conversation not found"})
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Conversation database unavailable: "+type(exc).__name__})
            return
        if route.path == "/api/conversations/rename":
            try:
                import app_db
                ok=app_db.rename_conversation(data.get("owner"),data.get("id"),data.get("title",""))
                self.json_reply(200,{"renamed":True}) if ok else self.json_reply(404,{"error":"Conversation not found"})
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Conversation database unavailable: "+type(exc).__name__})
            return
        if route.path == "/api/conversations/delete":
            try:
                import app_db
                ok=app_db.delete_conversation(data.get("owner"),data.get("id"))
                self.json_reply(200,{"deleted":True}) if ok else self.json_reply(404,{"error":"Conversation not found"})
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Conversation database unavailable: "+type(exc).__name__})
            return
        if route.path == "/api/rag/index":
            try:
                import rag
                self.json_reply(202,rag.start(data.get("paths")))
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except RuntimeError as exc:self.json_reply(409,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Indexing could not start: "+type(exc).__name__})
            return
        if route.path == "/api/rag/remove":
            try:
                import rag
                removed=rag.remove(data.get("path"))
                self.json_reply(200,{"removed":bool(removed)})
            except ValueError as exc:self.json_reply(400,{"error":str(exc)})
            except Exception as exc:self.json_reply(503,{"error":"Index removal failed: "+type(exc).__name__})
            return
        if route.path == "/api/agentic/ask":
            owner=data.get("owner"); cid=data.get("conversationId"); question=data.get("question"); provider=data.get("provider")
            request_id=data.get("requestId")
            if not isinstance(request_id,str) or not __import__("re").fullmatch(r"[A-Za-z0-9_-]{16,64}",request_id):
                request_id=secrets.token_hex(16)
            import agent_activity
            def activity(message,state="info",**details):
                state=details.pop("status",state)
                agent_activity.record(owner,message,state,request_id=request_id,conversation_id=cid,**details)
            activity("Request received","working",source="api",stage="request",overview=True)
            try:
                import app_db
                conversation=app_db.get_conversation(owner,cid)
                if not conversation: raise ValueError("Conversation not found.")
                if not isinstance(question,str) or not question.strip() or len(question)>1800: raise ValueError("Enter a message up to 1,800 characters.")
                selected=data.get("selected",[])
                if not isinstance(selected,list) or len(selected)>6: raise ValueError("Select up to six shared files.")
                model=data.get("model","")
                activity("Request schema and input validated",stage="validation",overview=True,metadata={"promptCharacters":len(question),"selectedFileCount":len(data.get("selected",[])) if isinstance(data.get("selected",[]),list) else None})
                if provider=="local":
                    import local_ai
                    spec=local_ai.MODELS.get(model)
                    if spec: activity("Using "+spec["name"],source="model",stage="model-selection",overview=True,metadata={"model":model})
                    activity("Request sent to local inference","working",source="local-ai",stage="inference",overview=True)
                    history=[m for m in conversation["messages"] if m["role"] in ("user","assistant")][-4:]
                    result=local_ai.chat(question,selected,history,model,cid,owner,activity=activity)
                elif provider=="openrouter":
                    from agent import ask
                    activity("Request sent to OpenRouter","working",source="openrouter",stage="inference",overview=True)
                    history=[m for m in conversation["messages"] if m["role"] in ("user","assistant")][-12:]
                    result=ask(question,self.client_address[0],model or "openrouter/free",selected,history)
                    answer=result.get("answer") if isinstance(result,dict) else None
                    if not isinstance(answer,str) or not answer.strip():
                        raise RuntimeError("The selected provider returned an empty answer.")
                    if len(answer)>app_db.MAX_MESSAGE:
                        marker="\n\n[Answer shortened to fit the 30,000-character conversation history limit.]"
                        answer=answer[:app_db.MAX_MESSAGE-len(marker)].rstrip()+marker
                        result["answer"]=answer
                    app_db.add_message(owner,cid,"user",question,model)
                    app_db.add_message(owner,cid,"assistant",answer,model,{"sources":result.get("sources",[])})
                else: raise ValueError("Select a supported provider.")
                sources=result.get("sources",[]) if isinstance(result,dict) else []
                if sources: activity(f"Used {len(sources)} selected or indexed source(s)")
                activity("Response generated successfully","success",source="api",stage="complete",overview=True)
                if isinstance(result,dict):result["requestId"]=request_id
                self.json_reply(200,result)
            except ValueError as exc:
                activity("Request rejected by validation","error",source="api",stage="validation",level="ERROR",overview=True)
                self.json_reply(400,{"error":str(exc)})
            except RuntimeError as exc:
                activity("Model or provider request failed","error",source="api",stage="inference",level="ERROR",overview=True)
                self.json_reply(503,{"error":str(exc)})
            except Exception as exc:
                activity("Request failed unexpectedly","error",source="api",stage="failure",level="ERROR",overview=True)
                print(f"Agentic chat failed: {type(exc).__name__}")
                self.json_reply(500,{"error":"The chat request failed. Check the selected provider status."})
            return
        if route.path == "/api/agent/ask":
            started = time.monotonic()
            question = data.get("question")
            selected = data.get("selected", [])
            model = data.get("model", "openrouter/free")
            event_fields = {"provider": "openrouter", "model": model,
                            "selected_count": len(selected) if isinstance(selected, list) else 0,
                            "query_chars": len(question) if isinstance(question, str) else 0}
            server_logs.record("agent.request.started", self.client_address[0], **event_fields)
            try:
                from agent import ask
                result = ask(question, self.client_address[0], model, selected)
            except (ValueError, RuntimeError) as exc:
                server_logs.record("agent.request.finished", self.client_address[0], **event_fields,
                                   outcome="error", error_type=type(exc).__name__,
                                   duration_ms=int((time.monotonic() - started) * 1000))
                self.json_reply(400 if isinstance(exc, ValueError) else 503, {"error": str(exc)})
                return
            except Exception as exc:
                server_logs.record("agent.request.finished", self.client_address[0], **event_fields,
                                   outcome="error", error_type=type(exc).__name__,
                                   duration_ms=int((time.monotonic() - started) * 1000))
                raise
            server_logs.record("agent.request.finished", self.client_address[0], **event_fields,
                               outcome="success", source_count=len(result.get("sources", [])),
                               duration_ms=int((time.monotonic() - started) * 1000))
            self.json_reply(200, result)
            return
        if route.path.startswith("/api/local-ai/"):
            action = route.path.removeprefix("/api/local-ai/")
            question = data.get("question")
            selected = data.get("selected", [])
            fields = {"provider": "local", "action": action,
                      "selected_count": len(selected) if isinstance(selected, list) else 0,
                      "query_chars": len(question) if isinstance(question, str) else 0,
                      "task": data.get("task") if action == "documents/summarize" else None}
            started = time.monotonic()
            server_logs.record("agent.local.started", self.client_address[0], **fields)
            try:
                import local_ai
                if action == "models/download": result = local_ai.start_download(data.get("model", local_ai.MODEL_ID))
                elif action == "models/activate": result = local_ai.activate(data.get("model"))
                elif action == "models/unload": result = local_ai.unload()
                elif action == "memory/optimize": result = local_ai.optimize_memory()
                elif action == "models/delete": result = local_ai.delete_model(data.get("model"), data.get("confirm"))
                elif action == "chat": result = local_ai.chat(question, selected, data.get("history", []), data.get("model"), data.get("conversationId"), data.get("owner"))
                elif action == "documents/summarize": result = local_ai.summarize_pdf(data.get("path"), data.get("task", "summary"))
                else:
                    server_logs.record("agent.local.finished", self.client_address[0], **fields, outcome="not_found")
                    self.json_reply(404, {"error": "Local AI route not found"})
                    return
            except ValueError as exc:
                server_logs.record("agent.local.finished", self.client_address[0], **fields, outcome="error",
                                   error_type=type(exc).__name__, duration_ms=int((time.monotonic() - started) * 1000))
                details = local_ai.record_load_error(data.get("model"), exc) if action == "models/activate" else None
                response = {"error": str(exc)}
                if details: response["diagnostics"] = details
                self.json_reply(400, response)
                return
            except RuntimeError as exc:
                server_logs.record("agent.local.finished", self.client_address[0], **fields, outcome="error",
                                   error_type=type(exc).__name__, duration_ms=int((time.monotonic() - started) * 1000))
                details = local_ai.record_load_error(data.get("model"), exc) if action == "models/activate" else None
                response = {"error": str(exc)}
                if details: response["diagnostics"] = details
                self.json_reply(503, response)
                return
            except Exception as exc:
                # Never record prompt text, document data, credentials, or arbitrary request values.
                server_logs.record("agent.local.finished", self.client_address[0], **fields, outcome="error",
                                   error_type=type(exc).__name__, duration_ms=int((time.monotonic() - started) * 1000))
                print(f"Local AI operation failed: {type(exc).__name__}")
                self.json_reply(500, {"error": "Local AI operation failed. Check the local AI status and logs."})
                return
            server_logs.record("agent.local.finished", self.client_address[0], **fields,
                               outcome="success", source_count=len(result.get("sources", [])) if isinstance(result, dict) else 0,
                               duration_ms=int((time.monotonic() - started) * 1000))
            self.json_reply(200, result)
            return
        if route.path.startswith("/api/direct-hosting/"):
            try:
                import direct_host
                action = route.path.removeprefix("/api/direct-hosting/")
                if action == "diagnose": result = direct_host.diagnose()
                elif action == "configure": result = direct_host.configure(data.get("hostname", ""))
                elif action == "start": result = direct_host._start_caddy(data.get("confirmHostname", ""))
                elif action == "stop": result = {"running": False, "stopped": direct_host._stop_pid(direct_host.CADDY_PID, str(direct_host.CADDYFILE.resolve()))}
                elif action == "restart":
                    direct_host._stop_pid(direct_host.CADDY_PID, str(direct_host.CADDYFILE.resolve()))
                    result = direct_host._start_caddy(data.get("confirmHostname", ""))
                elif action == "dns-update": result = direct_host.godaddy_update()
                elif action == "dns-start": result = direct_host.start_dns_watch()
                elif action == "dns-stop": result = direct_host.stop_dns_watch()
                else:
                    self.json_reply(404, {"error": "Unknown Direct Hosting action"})
                    return
            except ValueError as exc:
                self.json_reply(400, {"error": str(exc)})
                return
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                self.json_reply(503, {"error": str(exc)})
                return
            self.json_reply(200, result)
            return
        actions = {"/api/rename": self.rename_item, "/api/move": self.move_item,
                   "/api/delete": self.delete_item}
        action = actions.get(route.path)
        if action is None:
            self.reply(404, b"Not found")
        else:
            action(data)

    def make_directory(self, query):
        parent = safe_path(query_value(query, "path"))
        name = query_value(query, "name")
        if parent is None or not parent.is_dir() or not safe_leaf(name):
            self.json_reply(400, {"error": "Invalid folder name or location"})
            return
        target = parent / name
        try:
            target.mkdir()
        except FileExistsError:
            self.json_reply(409, {"error": "That name already exists"})
        except OSError:
            self.json_reply(400, {"error": "Could not create folder"})
        else:
            self.json_reply(201, {"created": name})

    def upload(self, query):
        parent = safe_path(query_value(query, "path"))
        name = query_value(query, "name")
        if parent is None or not parent.is_dir() or not safe_leaf(name):
            self.reply(400, b"Invalid upload name or folder")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.reply(411, b"Content-Length required")
            return
        if not 0 <= length <= MAX_UPLOAD:
            self.reply(413, b"Upload must be 512 MiB or smaller")
            return
        target = parent / name
        if target.exists() or target.is_symlink():
            self.reply(409, b"A file or folder with that name already exists")
            return
        temp = parent / ("." + secrets.token_hex(12) + ".upload")
        try:
            remaining = length
            with temp.open("xb") as stream:
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise OSError("incomplete upload")
                    stream.write(chunk)
                    remaining -= len(chunk)
            with MUTATION_LOCK:
                if target.exists() or target.is_symlink():
                    self.reply(409, b"A file or folder with that name already exists")
                    return
                os.rename(temp, target)
            self.reply(201, b"Uploaded")
        except OSError:
            self.reply(400, b"Upload failed")
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def rename_item(self, data):
        source = safe_path(data.get("path", ""), allow_root=False)
        name = data.get("name", "")
        if source is None or not source.exists() or source.is_symlink() or not safe_leaf(name):
            self.json_reply(400, {"error": "Invalid item or name"})
            return
        target = source.with_name(name)
        try:
            with MUTATION_LOCK:
                if target.exists() or target.is_symlink():
                    self.json_reply(409, {"error": "That name already exists"})
                    return
                source.rename(target)
        except OSError:
            self.json_reply(400, {"error": "Could not rename item"})
        else:
            try:
                from agent import rewrite_tags
                rewrite_tags(source.relative_to(ROOT).as_posix(), target.relative_to(ROOT).as_posix())
            except OSError:
                print("[WARN] File renamed, but its Agent tags could not be updated.", flush=True)
            self.json_reply(200, {"path": target.relative_to(ROOT).as_posix()})

    def move_item(self, data):
        source = safe_path(data.get("path", ""), allow_root=False)
        dest = safe_path(data.get("destination", ""))
        if source is None or dest is None or not source.exists() or source.is_symlink() or not dest.is_dir():
            self.json_reply(400, {"error": "Invalid source or destination"})
            return
        try:
            dest.resolve().relative_to(source.resolve())
            self.json_reply(400, {"error": "Cannot move a folder into itself"})
            return
        except ValueError:
            pass
        target = dest / source.name
        try:
            with MUTATION_LOCK:
                if target.exists() or target.is_symlink():
                    self.json_reply(409, {"error": "The destination already contains that name"})
                    return
                source.rename(target)
        except OSError:
            self.json_reply(400, {"error": "Could not move item"})
        else:
            try:
                from agent import rewrite_tags
                rewrite_tags(source.relative_to(ROOT).as_posix(), target.relative_to(ROOT).as_posix())
            except OSError:
                print("[WARN] File moved, but its Agent tags could not be updated.", flush=True)
            self.json_reply(200, {"path": target.relative_to(ROOT).as_posix()})

    def delete_item(self, data):
        target = safe_path(data.get("path", ""), allow_root=False)
        if target is None or not target.exists() or target.is_symlink():
            self.json_reply(404, {"error": "Item not found"})
            return
        try:
            with MUTATION_LOCK:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        except OSError:
            self.json_reply(400, {"error": "Could not delete item"})
        else:
            try:
                from agent import rewrite_tags
                rewrite_tags(target.relative_to(ROOT).as_posix())
            except OSError:
                print("[WARN] File deleted, but its Agent tags could not be cleaned up.", flush=True)
            self.json_reply(200, {"deleted": True})

    def __init__(self, *args, **kwargs):
        self._request_started = time.monotonic()
        self._audit_status = 0
        super().__init__(*args, **kwargs)

    def send_response(self, code, message=None):
        self._audit_status = code
        super().send_response(code, message)

    def log_message(self, fmt, *args):
        # Log route only: query strings may contain filenames or search terms.
        route = urlsplit(getattr(self, "path", "")).path[:160]
        if route != "/api/logs":
            try: body_bytes = int(self.headers.get("Content-Length", "0"))
            except (ValueError, AttributeError): body_bytes = 0
            server_logs.record("http.request", self.client_address[0], method=getattr(self, "command", ""),
                               route=route, http_status=self._audit_status, bytes=body_bytes,
                               duration_ms=int((time.monotonic() - self._request_started) * 1000))
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {getattr(self, 'command', '')} {route} {self._audit_status}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Share files with devices on your local Wi-Fi.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default=os.environ.get("MYFOREVER_BIND_ADDRESS", "0.0.0.0"),
                        help="bind address (default: 0.0.0.0; use 127.0.0.1 for local-only testing)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        import ipaddress
        ipaddress.ip_address(args.host)
    except ValueError:
        parser.error("host must be an IPv4 or IPv6 address")
    password = server_password_from_environment() or getpass.getpass("Choose a password for this session: ")
    if not password:
        parser.error("Password cannot be empty")
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        import app_db
        app_db.init()
    except Exception as exc:
        print(f"[WARN] Local metadata database unavailable: {type(exc).__name__}", flush=True)
    if ROOT.is_symlink():
        raise SystemExit("Refusing to serve a symbolic-link storage directory")
    try:
        server = ThreadingHTTPServer((args.host, args.port), FileHandler)
    except OSError as exc:
        raise SystemExit(f"Could not bind server: {exc}")
    server.password = password
    try:
        import hosting
        recovered=hosting.recover_applications()
        if recovered:print(f"Recovered {len(recovered)} previously active hosted application(s).",flush=True)
    except Exception as exc:
        print(f"[WARN] Hosted application recovery unavailable: {type(exc).__name__}",flush=True)
    print(f"Serving {ROOT} on 0.0.0.0:{args.port}")
    print("Open http://127.0.0.1:%d/ on this device." % args.port)
    if args.host in ("0.0.0.0", "::"):
        print("For other LAN devices, use this device's Wi-Fi IP and the same port.")
    print(f"Sign in as {USERNAME}. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
