#!/usr/bin/env python3
"""Authenticated image-generation UI and server-side API proxy."""

import base64
import hashlib
import hmac
import http.cookies
import cgi
import io
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlsplit


BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
LOGIN_FILE = BASE_DIR / "login.html"
DB_FILE = Path(os.environ.get("IMAGE_SITE_DB", "/var/lib/image-site/users.db"))
LISTEN_HOST = os.environ.get("IMAGE_SITE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("IMAGE_SITE_PORT", "4180"))
UPSTREAM = os.environ.get("IMAGE_SITE_UPSTREAM", "http://127.0.0.1:4141").rstrip("/")
API_KEY = os.environ.get("IMAGE_SITE_API_KEY", "")
SESSION_SECRET = os.environ.get("IMAGE_SITE_SESSION_SECRET", "").encode("utf-8")
COOKIE_NAME = "image_session"
SESSION_TTL = 12 * 60 * 60
MAX_BODY = 64 * 1024
MAX_IMAGE_SIZE = 20 * 1024 * 1024
MAX_MULTIPART_BODY = MAX_IMAGE_SIZE + 256 * 1024
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
PBKDF2_ROUNDS = 310000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
ALLOWED_SIZES = {"1024x1024", "1536x1024", "1024x1536"}


def relativize_upstream_urls(obj):
    """Rewrite gateway-absolute URLs to origin-relative paths.

    The gateway builds image file URLs from the Host header it saw, which from
    behind this proxy is loopback (127.0.0.1:4141) or a container service name.
    Stripping the UPSTREAM origin turns them into /v1/... paths that the browser
    resolves against whatever public origin the site is served from, so no
    domain needs to be hardcoded. Upstream CDN URLs are left untouched.
    """
    prefix = UPSTREAM + "/"
    if isinstance(obj, str):
        if obj.startswith(prefix):
            return obj[len(UPSTREAM):]
        return obj
    if isinstance(obj, list):
        return [relativize_upstream_urls(item) for item in obj]
    if isinstance(obj, dict):
        return {key: relativize_upstream_urls(value) for key, value in obj.items()}
    return obj


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Python 3.6-compatible threaded HTTP server."""

    daemon_threads = True


def db_connect():
    connection = sqlite3.connect(str(DB_FILE), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def password_verify(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.urlsafe_b64decode(salt.encode("ascii")),
            int(rounds),
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected.encode("ascii")))
    except (ValueError, TypeError):
        return False


def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with db_connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
            "session_version INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL)"
        )


def create_initial_user(username, password):
    if not USERNAME_RE.fullmatch(username) or len(password) < 16:
        raise ValueError("invalid initial credentials")
    with db_connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, username, password_hash, updated_at) VALUES (1, ?, ?, ?)",
            (username, password_hash(password), int(time.time())),
        )


def session_token(user):
    payload = "{}:{}:{}".format(user["id"], user["session_version"], int(time.time()) + SESSION_TTL)
    encoded = base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")
    signature = hmac.new(SESSION_SECRET, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def session_user(token):
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        user_id, version, expires = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii").split(":")
        if int(expires) < int(time.time()):
            return None
        with db_connect() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if not user or user["session_version"] != int(version):
            return None
        return user
    except (ValueError, TypeError, UnicodeError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ImageSite"

    def log_message(self, fmt, *args):
        # Never log request bodies, credentials, cookies, or the upstream key.
        print("{} - {}".format(self.client_address[0], fmt % args), flush=True)

    def security_headers(self, content_type=None):
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )

    def json_response(self, status, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.security_headers("application/json; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def html_response(self, status, path):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(500)
            return
        self.send_response(status)
        self.security_headers("text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("无效请求")
        if length <= 0 or length > MAX_BODY:
            raise ValueError("请求内容大小无效")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError("JSON 格式无效")
        if not isinstance(value, dict):
            raise ValueError("请求内容无效")
        return value

    def current_user(self):
        raw = self.headers.get("Cookie", "")
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(raw)
            token = cookie.get(COOKIE_NAME)
            return session_user(token.value) if token else None
        except http.cookies.CookieError:
            return None

    def require_user(self):
        user = self.current_user()
        if not user:
            self.json_response(401, {"error": {"message": "请先登录", "type": "auth_error"}})
            return None
        return user

    def cookie_value(self, token, max_age=SESSION_TTL):
        return "{}={}; Path=/image/; Max-Age={}; HttpOnly; Secure; SameSite=Strict".format(
            COOKIE_NAME, token, max_age
        )

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/image", "/image/"):
            if not self.current_user():
                self.send_response(302)
                self.security_headers()
                self.send_header("Location", "/image/login")
                self.end_headers()
                return
            self.html_response(200, INDEX_FILE)
            return
        if path == "/image/login":
            if self.current_user():
                self.send_response(302)
                self.security_headers()
                self.send_header("Location", "/image/")
                self.end_headers()
                return
            self.html_response(200, LOGIN_FILE)
            return
        if path == "/image/api/session":
            user = self.require_user()
            if user:
                self.json_response(200, {"authenticated": True, "username": user["username"]})
            return
        if path == "/image/api/accounts":
            if not self.require_user():
                return
            self.proxy("GET", "/v1/accounts", None)
            return
        self.send_error(404)

    def read_multipart_edit(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("无效请求大小")
        if length <= 0 or length > MAX_MULTIPART_BODY:
            raise ValueError("请求总大小超过限制")
        content_type = self.headers.get("Content-Type", "")
        media_type, params = cgi.parse_header(content_type)
        if media_type.lower() != "multipart/form-data" or not params.get("boundary"):
            raise ValueError("必须使用 multipart/form-data")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("请求内容不完整")
        env = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(length)}
        form = cgi.FieldStorage(fp=io.BytesIO(body), headers=self.headers, environ=env, keep_blank_values=True)
        image = form["image"] if "image" in form else None
        if image is None or not getattr(image, "file", None):
            raise ValueError("请选择要编辑的图片")
        image_data = image.file.read(MAX_IMAGE_SIZE + 1)
        if not image_data or len(image_data) > MAX_IMAGE_SIZE:
            raise ValueError("图片大小必须在 20 MiB 以内")
        declared = (getattr(image, "type", "") or "").lower().split(";")[0].strip()
        if declared in ("image/jpg", "image/pjpeg"):
            declared = "image/jpeg"
        detected = None
        if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
            detected = "image/png"
        elif image_data.startswith(b"\xff\xd8\xff"):
            detected = "image/jpeg"
        elif len(image_data) >= 12 and image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
            detected = "image/webp"
        neutral_types = ("", "text/plain", "application/octet-stream")
        if detected not in ALLOWED_IMAGE_MIMES or (declared not in neutral_types and declared != detected):
            raise ValueError("仅支持签名有效的 PNG、JPEG 或 WebP 图片")
        def field(name, default=""):
            if name not in form:
                return default
            item = form[name]
            return item.value if isinstance(item.value, str) else default
        prompt = field("prompt").strip()
        size = field("size")
        count_raw = field("n")
        account_id = field("accountId").strip()
        if not prompt or len(prompt) > 4000:
            raise ValueError("提示词长度必须为 1 至 4000 个字符")
        try:
            count = int(count_raw)
        except (TypeError, ValueError):
            raise ValueError("生成数量无效")
        if size not in ALLOWED_SIZES or count < 1 or count > 4:
            raise ValueError("尺寸或生成数量无效")
        if len(account_id) > 256:
            raise ValueError("账号标识无效")
        filename = os.path.basename(getattr(image, "filename", "") or "image")[:255]
        return image_data, filename, detected, prompt, size, count, account_id

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/image/api/edit":
            user = self.current_user()
            if not user:
                self.json_response(401, {"error": {"message": "请先登录"}})
                return
            try:
                image_data, filename, mime, prompt, size, count, account_id = self.read_multipart_edit()
            except ValueError as exc:
                self.json_response(400, {"error": {"message": str(exc)}})
                return
            fields = {"prompt": prompt, "size": size, "n": str(count), "response_format": "url"}
            if account_id:
                fields["accountId"] = account_id
            self.proxy_multipart_edit(image_data, filename, mime, fields)
            return
        try:
            data = self.read_json()
        except ValueError as exc:
            self.json_response(400, {"error": {"message": str(exc)}})
            return

        if path == "/image/api/login":
            username = str(data.get("username", ""))
            password = str(data.get("password", ""))
            with db_connect() as db:
                user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if not user or not password_verify(password, user["password_hash"]):
                time.sleep(0.35)
                self.json_response(401, {"error": {"message": "用户名或密码错误"}})
                return
            self.json_response(200, {"ok": True}, self.cookie_value(session_token(user)))
            return

        user = self.require_user()
        if not user:
            return

        if path == "/image/api/logout":
            self.json_response(200, {"ok": True}, self.cookie_value("deleted", 0))
            return

        if path == "/image/api/account":
            username = str(data.get("username", "")).strip()
            current_password = str(data.get("currentPassword", ""))
            new_password = str(data.get("newPassword", ""))
            if not USERNAME_RE.fullmatch(username):
                self.json_response(400, {"error": {"message": "用户名须为 3 至 64 位字母、数字、点、横线或下划线"}})
                return
            if not password_verify(current_password, user["password_hash"]):
                self.json_response(403, {"error": {"message": "当前密码错误"}})
                return
            if len(new_password) < 16:
                self.json_response(400, {"error": {"message": "新密码至少需要 16 个字符"}})
                return
            with db_connect() as db:
                db.execute(
                    "UPDATE users SET username = ?, password_hash = ?, session_version = session_version + 1, updated_at = ? WHERE id = ?",
                    (username, password_hash(new_password), int(time.time()), user["id"]),
                )
                changed = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            self.json_response(200, {"ok": True, "username": username}, self.cookie_value(session_token(changed)))
            return

        if path == "/image/api/generate":
            prompt = data.get("prompt")
            size = data.get("size")
            count = data.get("n")
            account_id = data.get("accountId")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
                self.json_response(400, {"error": {"message": "提示词长度必须为 1 至 4000 个字符"}})
                return
            if size not in ALLOWED_SIZES or not isinstance(count, int) or count < 1 or count > 4:
                self.json_response(400, {"error": {"message": "尺寸或生成数量无效"}})
                return
            payload = {
                "model": "gpt-image-2",
                "prompt": prompt.strip(),
                "size": size,
                "n": count,
                "response_format": "url",
            }
            if isinstance(account_id, str) and 0 < len(account_id) <= 256:
                payload["accountId"] = account_id
            self.proxy("POST", "/v1/images/generations", payload)
            return

        self.send_error(404)

    def proxy_multipart_edit(self, image_data, filename, mime, fields):
        if not API_KEY:
            self.json_response(503, {"error": {"message": "服务端图像 API 尚未配置"}})
            return
        boundary = "----ImageSite" + secrets.token_hex(16)
        chunks = []
        for name, value in fields.items():
            chunks.extend([
                ("--" + boundary + "\r\n").encode("ascii"),
                ('Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name)).encode("ascii"),
                value.encode("utf-8"), b"\r\n"
            ])
        safe_filename = filename.replace('"', '_').replace("\r", "_").replace("\n", "_")
        chunks.extend([
            ("--" + boundary + "\r\n").encode("ascii"),
            ('Content-Disposition: form-data; name="image"; filename="{}"\r\n'.format(safe_filename)).encode("utf-8"),
            ("Content-Type: " + mime + "\r\n\r\n").encode("ascii"),
            image_data, b"\r\n", ("--" + boundary + "--\r\n").encode("ascii")
        ])
        body = b"".join(chunks)
        request = urllib.request.Request(
            UPSTREAM + "/v1/images/edits", data=body, method="POST",
            headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        self.send_upstream(request)

    def send_upstream(self, request):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                result = response.read(20 * 1024 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            result = exc.read(1024 * 1024)
            status = exc.code
        except Exception:
            self.json_response(502, {"error": {"message": "图像服务暂时不可用"}})
            return
        try:
            parsed = relativize_upstream_urls(json.loads(result.decode("utf-8")))
        except (ValueError, UnicodeError):
            self.json_response(502, {"error": {"message": "图像服务返回了无效响应"}})
            return
        self.json_response(status, parsed)

    def proxy(self, method, upstream_path, payload):
        if not API_KEY:
            self.json_response(503, {"error": {"message": "服务端图像 API 尚未配置"}})
            return
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            UPSTREAM + upstream_path,
            data=body,
            method=method,
            headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"},
        )
        self.send_upstream(request)


def main():
    if len(SESSION_SECRET) < 32:
        raise SystemExit("IMAGE_SITE_SESSION_SECRET must be at least 32 bytes")
    init_db()
    initial_user = os.environ.get("IMAGE_SITE_INITIAL_USERNAME")
    initial_password = os.environ.get("IMAGE_SITE_INITIAL_PASSWORD")
    if initial_user and initial_password:
        create_initial_user(initial_user, initial_password)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    print("image-site listening on {}:{}".format(LISTEN_HOST, LISTEN_PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
