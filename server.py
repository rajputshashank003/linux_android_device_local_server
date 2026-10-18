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
import secrets
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit, parse_qs

USERNAME = "admin"
ROOT = Path(__file__).resolve().parent / "shared"
UI_FILE = Path(__file__).resolve().parent / "ui.html"
MAX_UPLOAD = 512 * 1024 * 1024
CHUNK_SIZE = 128 * 1024
MUTATION_LOCK = threading.RLock()
CLIPBOARD_LOCK = threading.RLock()
CLIPBOARD_TEXT = ""
CLIPBOARD_UPDATED = None
MAX_CLIPBOARD_BYTES = 32768
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

    def json_reply(self, status, obj):
        self.reply(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if not self.authenticate():
            return
        route = urlsplit(self.path)
        q = route.query
        if route.path == "/":
            try:
                page = UI_FILE.read_bytes()
            except OSError:
                self.reply(500, b"UI file is missing")
                return
            self.reply(200, page, "text/html; charset=utf-8", (("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; img-src 'self' blob: data:; media-src 'self' blob:; frame-src 'self' blob:; object-src 'none'; base-uri 'none'"),))
        elif route.path == "/api/capabilities":
            self.json_reply(200, {"management": True, "preview": True, "range": True, "zip": True, "clipboard": True, "chat": True, "pdfjs": True})
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
        elif route.path == "/api/mkdir":
            self.make_directory(route.query)
        elif route.path == "/api/upload":
            self.upload(route.query)
        else:
            self.reply(404, b"Not found")

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 <= length <= 65536:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (UnicodeError, json.JSONDecodeError):
            return None

    def do_POST(self):
        if not self.authenticate():
            return
        route = urlsplit(self.path)
        data = self.read_json()
        if data is None:
            self.json_reply(400, {"error": "Invalid JSON request"})
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
            self.json_reply(200, {"deleted": True})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Share files with devices on your local Wi-Fi.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    password = os.environ.get("TERMUX_SERVER_PASSWORD") or getpass.getpass("Choose a password for this session: ")
    if not password:
        parser.error("Password cannot be empty")
    ROOT.mkdir(parents=True, exist_ok=True)
    if ROOT.is_symlink():
        raise SystemExit("Refusing to serve a symbolic-link storage directory")
    try:
        server = ThreadingHTTPServer(("0.0.0.0", args.port), FileHandler)
    except OSError as exc:
        raise SystemExit(f"Could not bind server: {exc}")
    server.password = password
    print(f"Serving {ROOT} on 0.0.0.0:{args.port}")
    print("Open http://127.0.0.1:%d/ here, or this device's Wi-Fi IP on another device." % args.port)
    print(f"Sign in as {USERNAME}. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
