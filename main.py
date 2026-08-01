import hashlib
import io
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from io import BytesIO
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    Response,
)
from typing import List, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool
import boto3
from botocore.config import Config as BotoConfig
from PIL import Image

# ----------------------------------------------------------------------------
# Paths & configuration
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
ANDROID_APK_PATH = os.path.join(STATIC_DIR, "android", "score-player.apk")
APP_VERSION = os.environ.get("SCORE_APP_VERSION", "1.3.0")
# Runtime data dir is only used for transient ffmpeg temp files now
# (all persistent files live in Backblaze B2).
DATA_DIR = os.environ.get("SCORE_DATA_DIR", "/tmp/score_app_data")
os.makedirs(DATA_DIR, exist_ok=True)

# --- Database (Supabase PostgreSQL) -----------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Object storage (Backblaze B2, S3-compatible) ---------------------------
B2_KEY_ID = os.environ.get("B2_KEY_ID", "")
B2_APP_KEY = os.environ.get("B2_APP_KEY", "")
B2_ENDPOINT = os.environ.get("B2_ENDPOINT", "")  # e.g. s3.ca-east-006.backblazeb2.com
B2_BUCKET = os.environ.get("B2_BUCKET", "")
B2_REGION = os.environ.get("B2_REGION", "")  # e.g. ca-east-006 (auto-derived if empty)
# Presigned URL lifetime (S3 max is 7 days = 604800s).
PRESIGN_TTL = int(os.environ.get("B2_PRESIGN_TTL", str(7 * 24 * 3600)))

# Fixed admin credentials. Only the PBKDF2 hash is stored (encrypted at rest).
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_SALT = os.environ.get("ADMIN_SALT", "151ef02104f1cd54a042d2295e9929b8")
ADMIN_HASH = os.environ.get(
    "ADMIN_HASH",
    "05bc42913eabcaf3539599387c1380eb9e67d9484b654650403b3c1a1ae381ba",
)
PBKDF2_ROUNDS = 200000
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days
SESSION_COOKIE = "sid"

ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv"}


def _ctype(fname: str) -> str:
    c, _ = mimetypes.guess_type(fname)
    return c or "application/octet-stream"


# ----------------------------------------------------------------------------
# Backblaze B2 (S3) storage helpers
# ----------------------------------------------------------------------------
def _b2_endpoint_url() -> str:
    ep = B2_ENDPOINT.strip()
    if ep and not ep.startswith("http"):
        ep = "https://" + ep
    return ep


def _b2_region() -> str:
    if B2_REGION:
        return B2_REGION
    m = re.match(r"(?:https?://)?s3\.([^.]+)\.backblazeb2\.com", B2_ENDPOINT or "")
    return m.group(1) if m else "us-east-005"


_s3_client = None


def get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            endpoint_url=_b2_endpoint_url(),
            aws_access_key_id=B2_KEY_ID,
            aws_secret_access_key=B2_APP_KEY,
            region_name=_b2_region(),
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _s3_client


def _score_key(score_id: int, filename: str) -> str:
    return f"scores/{score_id}/{filename}"


def b2_upload_bytes(key: str, data: bytes, content_type: str) -> None:
    get_s3().put_object(
        Bucket=B2_BUCKET, Key=key, Body=data, ContentType=content_type
    )


def b2_read_bytes(key: str) -> bytes:
    obj = get_s3().get_object(Bucket=B2_BUCKET, Key=key)
    try:
        return obj["Body"].read()
    finally:
        obj["Body"].close()


def b2_presigned_url(key: str, ttl: int = PRESIGN_TTL) -> str:
    return get_s3().generate_presigned_url(
        "get_object", Params={"Bucket": B2_BUCKET, "Key": key}, ExpiresIn=ttl
    )


def b2_list_keys(prefix: str) -> List[str]:
    s3 = get_s3()
    token = None
    keys: List[str] = []
    while True:
        kwargs = {"Bucket": B2_BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return keys


def b2_delete_keys(keys: List[str]) -> None:
    if not keys:
        return
    s3 = get_s3()
    for i in range(0, len(keys), 1000):
        objs = [{"Key": k} for k in keys[i : i + 1000]]
        s3.delete_objects(Bucket=B2_BUCKET, Delete={"Objects": objs})


def b2_delete_key(key: str) -> None:
    get_s3().delete_object(Bucket=B2_BUCKET, Key=key)


def b2_delete_prefix(prefix: str) -> None:
    b2_delete_keys(b2_list_keys(prefix))


# ----------------------------------------------------------------------------
# ffmpeg helpers (video -> audio extraction & trimming)
# ----------------------------------------------------------------------------
def get_ffmpeg() -> Optional[str]:
    """Resolve an ffmpeg executable, preferring a system binary, then the
    bundled imageio-ffmpeg binary. Returns None if unavailable."""
    env_bin = os.environ.get("FFMPEG_BINARY")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    sys_bin = shutil.which("ffmpeg")
    if sys_bin:
        return sys_bin
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def process_media_to_mp3(
    src_path: str,
    dest_path: str,
    trim_start: Optional[float],
    trim_end: Optional[float],
) -> None:
    """Extract/convert the audio track of any audio or video file into an mp3,
    optionally trimming to [trim_start, trim_end] (seconds). Raises on failure."""
    ffmpeg = get_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("服务器未安装 ffmpeg，无法处理音视频")

    cmd = [ffmpeg, "-y", "-i", src_path]
    if trim_start is not None and trim_start > 0:
        cmd += ["-ss", f"{trim_start:.3f}"]
    if trim_end is not None and trim_end > 0:
        cmd += ["-to", f"{trim_end:.3f}"]
    cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2", dest_path]

    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600
    )
    if proc.returncode != 0 or not os.path.isfile(dest_path):
        tail = proc.stderr.decode("utf-8", "ignore")[-800:]
        raise RuntimeError(f"ffmpeg 处理失败: {tail}")


# ----------------------------------------------------------------------------
# Database helpers (Supabase PostgreSQL via psycopg2)
# ----------------------------------------------------------------------------
_pg_pool = None


def _dsn() -> str:
    """Return the DSN, ensuring SSL is required (Supabase mandates SSL)."""
    dsn = DATABASE_URL
    if dsn and "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    return dsn


def get_pool():
    global _pg_pool
    if _pg_pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 未配置")
        _pg_pool = pg_pool.ThreadedConnectionPool(1, 10, dsn=_dsn())
    return _pg_pool


@contextmanager
def db_conn():
    p = get_pool()
    conn = p.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


def db_query(sql: str, params=(), one: bool = False):
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return None
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else rows


def init_db() -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at BIGINT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    created_at BIGINT NOT NULL,
                    expires_at BIGINT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scores (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    audio_filename TEXT,
                    owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    created_at BIGINT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    id SERIAL PRIMARY KEY,
                    score_id INTEGER NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
                    page_index INTEGER NOT NULL,
                    image_filename TEXT NOT NULL,
                    turn_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS score_jobs (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress INTEGER NOT NULL DEFAULT 0,
                    score_id INTEGER,
                    error_msg TEXT,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_score_jobs_user_created
                    ON score_jobs (user_id, created_at DESC);
                """
            )
            # --- migrations for databases created before the multi-user feature ---
            cur.execute(
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;"
            )
            cur.execute(
                "ALTER TABLE scores ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE;"
            )
            # drop stale sessions that predate the users table (user_id NULL)
            cur.execute("DELETE FROM sessions WHERE user_id IS NULL;")
            # --- seed the initial super administrator ---
            cur.execute("SELECT COUNT(*) FROM users WHERE username = %s", (ADMIN_USERNAME,))
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO users (username, salt, password_hash, role, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (ADMIN_USERNAME, ADMIN_SALT, ADMIN_HASH, "superadmin", int(time.time())),
                )


def cleanup_sessions() -> None:
    db_query("DELETE FROM sessions WHERE expires_at < %s", (int(time.time()),))


# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------
def make_salt() -> str:
    return secrets.token_hex(16)


def hash_pw(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS
    ).hex()


def verify_pw(password: str, salt_hex: str, expected_hash: str) -> bool:
    try:
        return secrets.compare_digest(hash_pw(password, salt_hex), expected_hash)
    except Exception:
        return False


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db_query(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
        (token, user_id, now, now + SESSION_TTL_SECONDS),
    )
    return token


def get_session_user(token: Optional[str]) -> Optional[dict]:
    """Resolve a session cookie to a live user, or None if invalid/expired."""
    if not token:
        return None
    try:
        row = db_query(
            "SELECT s.expires_at, u.id, u.username, u.role "
            "FROM sessions s JOIN users u ON s.user_id = u.id "
            "WHERE s.token = %s",
            (token,),
            one=True,
        )
    except Exception:
        return None
    if not row or int(row["expires_at"]) < int(time.time()):
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def destroy_session(token: Optional[str]) -> None:
    if not token:
        return
    db_query("DELETE FROM sessions WHERE token = %s", (token,))


def current_user(request: Request) -> Optional[dict]:
    return getattr(request.state, "user", None)


def is_superadmin(request: Request) -> bool:
    u = current_user(request)
    return bool(u and u.get("role") == "superadmin")


# ----------------------------------------------------------------------------
# App & middleware
# ----------------------------------------------------------------------------
app = FastAPI(
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS: the offline Android build loads the frontend from file:///android_asset/
# (origin "null") and calls this backend cross-origin. Because we authenticate
# with a Bearer token (not cookies), allow_credentials must be False so that
# allow_origins=["*"] is actually honoured by the browser. Some Android
# WebView/file:// requests send the literal Origin header "null", so keep it
# explicitly listed as well.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_EXACT = {
    "/login",
    "/api/login",
    "/api",
    "/api/v1/ping",
    "/api/version",
    "/api/images/auto-crop",
    "/favicon.ico",
    "/sw.js",
    "/download/android",
    "/downloads",
}
# "/api/media/" is public: it only issues a 302 redirect to a short-lived
# presigned B2 URL. <img>/<audio> tags cannot carry the Bearer header, so the
# offline app needs to reach media without an Authorization header.
PUBLIC_PREFIX = ("/assets/", "/static/", "/api/docs", "/api/redoc", "/api/openapi.json", "/api/media/")


def _extract_token(request: Request) -> Optional[str]:
    """Resolve the session token from either the Bearer header (offline app) or
    the ``sid`` cookie (legacy web, backward compatible)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
            if token:
                return token
    return request.cookies.get(SESSION_COOKIE)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_EXACT or any(path.startswith(p) for p in PUBLIC_PREFIX):
        return await call_next(request)
    user = get_session_user(_extract_token(request))
    if user:
        request.state.user = user
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
    return RedirectResponse(url="/login", status_code=302)


# ----------------------------------------------------------------------------
# Static asset helpers
# ----------------------------------------------------------------------------
def serve_static_file(filename: str) -> Response:
    full = os.path.join(STATIC_DIR, filename)
    if not os.path.isfile(full):
        return Response(status_code=404, content="Not Found")
    ctype, _ = mimetypes.guess_type(full)
    return FileResponse(full, media_type=ctype or "application/octet-stream")


def serve_page(filename: str) -> HTMLResponse:
    full = os.path.join(STATIC_DIR, filename)
    if not os.path.isfile(full):
        return HTMLResponse(status_code=404, content="Not Found")
    with open(full, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ----------------------------------------------------------------------------
# Page routes
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home_page():
    return serve_page("home.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return serve_page("login.html")


@app.get("/new", response_class=HTMLResponse)
async def new_page():
    return serve_page("new.html")


@app.get("/player/{score_id}", response_class=HTMLResponse)
async def player_page(score_id: int):
    return serve_page("player.html")


@app.get("/users", response_class=HTMLResponse)
async def users_page():
    return serve_page("users.html")


@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page():
    return serve_page("downloads.html")


@app.get("/assets/{filename:path}")
async def assets(filename: str):
    return serve_static_file(filename)


@app.get("/static/{filename:path}")
async def static_files(filename: str):
    return serve_static_file(filename)


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        os.path.join(STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/api/version")
async def app_version():
    return {"version": APP_VERSION, "apk_url": "https://score-player.onrender.com/download/android"}


@app.head("/download/android")
@app.get("/download/android")
async def download_android_app():
    if not os.path.isfile(ANDROID_APK_PATH):
        return Response(status_code=404, content="Android APK not found")
    return FileResponse(
        ANDROID_APK_PATH,
        media_type="application/vnd.android.package-archive",
        filename="score-player.apk",
        headers={"Content-Disposition": 'attachment; filename="score-player.apk"'},
    )


@app.get("/favicon.ico")
async def favicon():
    full = os.path.join(STATIC_DIR, "favicon.ico")
    if os.path.isfile(full):
        return FileResponse(full)
    return Response(status_code=204)


# ----------------------------------------------------------------------------
# Auth API
# ----------------------------------------------------------------------------
@app.get("/api")
async def index_handler():
    return {"app": "score_app", "status": "ok"}


@app.get("/api/v1/ping")
async def ping():
    return "pong"


@app.post("/api/login")
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = {"username": form.get("username"), "password": form.get("password")}

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    row = db_query(
        "SELECT id, salt, password_hash FROM users WHERE username = %s",
        (username,),
        one=True,
    )
    if not row or not verify_pw(password, row["salt"], row["password_hash"]):
        return JSONResponse(status_code=401, content={"detail": "用户名或密码错误"})

    cleanup_sessions()
    token = create_session(row["id"])
    # Dual-mode: return the token in JSON (offline app stores it as a Bearer
    # token in localStorage) AND set the httponly cookie (legacy web version).
    resp = JSONResponse(content={"ok": True, "token": token})
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = _extract_token(request)
    destroy_session(token)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/session")
async def api_session(request: Request):
    u = current_user(request)
    return {"authenticated": True, "username": u["username"], "role": u["role"]}


# ----------------------------------------------------------------------------
# User management API (superadmin) + self-service password change
# ----------------------------------------------------------------------------
VALID_ROLES = ("superadmin", "user")


@app.get("/api/users")
async def api_list_users(request: Request):
    if not is_superadmin(request):
        return JSONResponse(status_code=403, content={"detail": "需要超级管理员权限"})
    rows = db_query(
        "SELECT id, username, role, created_at FROM users ORDER BY id"
    )
    return {
        "users": [
            {
                "id": r["id"],
                "username": r["username"],
                "role": r["role"],
                "created_at_text": _fmt_time(r["created_at"]),
            }
            for r in (rows or [])
        ]
    }


@app.post("/api/users")
async def api_create_user(request: Request):
    if not is_superadmin(request):
        return JSONResponse(status_code=403, content={"detail": "需要超级管理员权限"})
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "user"
    if not username:
        return JSONResponse(status_code=400, content={"detail": "用户名不能为空"})
    if role not in VALID_ROLES:
        return JSONResponse(status_code=400, content={"detail": "角色无效"})
    if len(password) < 6:
        return JSONResponse(status_code=400, content={"detail": "密码至少 6 位"})
    if db_query("SELECT id FROM users WHERE username = %s", (username,), one=True):
        return JSONResponse(status_code=400, content={"detail": "用户名已存在"})
    salt = make_salt()
    db_query(
        "INSERT INTO users (username, salt, password_hash, role, created_at) VALUES (%s, %s, %s, %s, %s)",
        (username, salt, hash_pw(password, salt), role, int(time.time())),
    )
    return {"ok": True}


@app.delete("/api/users/{user_id}")
async def api_delete_user(request: Request, user_id: int):
    me = current_user(request)
    if not is_superadmin(request):
        return JSONResponse(status_code=403, content={"detail": "需要超级管理员权限"})
    if user_id == me["id"]:
        return JSONResponse(status_code=400, content={"detail": "不能删除当前登录的自己"})
    row = db_query("SELECT id, role FROM users WHERE id = %s", (user_id,), one=True)
    if not row:
        return JSONResponse(status_code=404, content={"detail": "用户不存在"})
    if row["role"] == "superadmin":
        cnt = db_query(
            "SELECT COUNT(*) AS c FROM users WHERE role = 'superadmin'", one=True
        )["c"]
        if cnt <= 1:
            return JSONResponse(
                status_code=400, content={"detail": "不能删除最后一个超级管理员"}
            )
    # collect and remove the user's score objects before DB cascade to avoid B2 orphans
    sids = db_query("SELECT id FROM scores WHERE owner_id = %s", (user_id,)) or []
    keys_to_delete: List[str] = []
    for s in sids:
        keys_to_delete.extend(b2_list_keys(f"scores/{s['id']}/"))
    b2_delete_keys(keys_to_delete)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))  # cascades scores/pages/sessions
    return {"ok": True, "b2_deleted": len(keys_to_delete)}


@app.post("/api/users/{user_id}/password")
async def api_reset_user_password(request: Request, user_id: int):
    if not is_superadmin(request):
        return JSONResponse(status_code=403, content={"detail": "需要超级管理员权限"})
    body = await request.json()
    new_pw = body.get("new_password") or ""
    if len(new_pw) < 6:
        return JSONResponse(status_code=400, content={"detail": "新密码至少 6 位"})
    if not db_query("SELECT id FROM users WHERE id = %s", (user_id,), one=True):
        return JSONResponse(status_code=404, content={"detail": "用户不存在"})
    salt = make_salt()
    db_query(
        "UPDATE users SET salt = %s, password_hash = %s WHERE id = %s",
        (salt, hash_pw(new_pw, salt), user_id),
    )
    # force re-login for that user
    db_query("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    return {"ok": True}


@app.post("/api/me/password")
async def api_change_my_password(request: Request):
    me = current_user(request)
    body = await request.json()
    old_pw = body.get("old_password") or ""
    new_pw = body.get("new_password") or ""
    if len(new_pw) < 6:
        return JSONResponse(status_code=400, content={"detail": "新密码至少 6 位"})
    row = db_query(
        "SELECT salt, password_hash FROM users WHERE id = %s", (me["id"],), one=True
    )
    if not row or not verify_pw(old_pw, row["salt"], row["password_hash"]):
        return JSONResponse(status_code=400, content={"detail": "原密码错误"})
    salt = make_salt()
    db_query(
        "UPDATE users SET salt = %s, password_hash = %s WHERE id = %s",
        (salt, hash_pw(new_pw, salt), me["id"]),
    )
    return {"ok": True}


# ----------------------------------------------------------------------------
# Scores API
# ----------------------------------------------------------------------------
def _fmt_time(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M")


API_CACHE_TTL_SECONDS = 60
_api_cache = {}


def _cache_get(key):
    item = _api_cache.get(key)
    if not item:
        return None
    cached_at, value = item
    if time.time() - cached_at > API_CACHE_TTL_SECONDS:
        _api_cache.pop(key, None)
        return None
    return value


def _cache_set(key, value):
    _api_cache[key] = (time.time(), value)
    return value


def _invalidate_score_cache(score_id: Optional[int] = None) -> None:
    for key in list(_api_cache.keys()):
        if key[0] == "scores_list" or (score_id is not None and key[0] == "score_detail" and key[1] == score_id):
            _api_cache.pop(key, None)


# ----------------------------------------------------------------------------
# Async score-creation jobs (score_jobs table)
# ----------------------------------------------------------------------------
_JOB_UPDATABLE_FIELDS = {"status", "progress", "score_id", "error_msg"}


def create_job(user_id: int, title: str) -> str:
    """Insert a fresh pending job row and return its id (uuid hex)."""
    job_id = uuid.uuid4().hex
    now = int(time.time())
    db_query(
        "INSERT INTO score_jobs (id, user_id, title, status, progress, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'pending', 0, %s, %s)",
        (job_id, user_id, title, now, now),
    )
    return job_id


def update_job(job_id: str, **fields) -> None:
    """Update a job's status/progress/score_id/error_msg + updated_at."""
    sets, params = [], []
    for k, v in fields.items():
        if k in _JOB_UPDATABLE_FIELDS:
            sets.append(f"{k} = %s")
            params.append(v)
    sets.append("updated_at = %s")
    params.append(int(time.time()))
    params.append(job_id)
    db_query(f"UPDATE score_jobs SET {', '.join(sets)} WHERE id = %s", tuple(params))


def _job_to_dict(row: dict) -> dict:
    return {
        "job_id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "progress": row["progress"],
        "score_id": row["score_id"],
        "error_msg": row["error_msg"],
        "created_at": row["created_at"],
        "created_at_text": _fmt_time(row["created_at"]),
        "updated_at": row["updated_at"],
    }


def process_score_job(
    job_id: str,
    owner_id: int,
    name: str,
    mode: str,
    page_rows: list,
    audio_raw: Optional[bytes],
    audio_src_ext: Optional[str],
    t_start: Optional[float],
    t_end: Optional[float],
) -> None:
    """Run the heavy score-creation work in the background (threadpool).

    ``page_rows`` is a list of ``(idx, fname, ts, content_bytes)`` tuples whose
    bytes have already been read from the request (UploadFile is closed once the
    response returns, so all reads must happen in the request handler)."""
    uploaded_keys: List[str] = []
    try:
        update_job(job_id, status="processing", progress=10)

        # 1) Process audio/video -> mp3 bytes (if provided)
        audio_filename = None
        audio_bytes = None
        if audio_raw is not None:
            src_ext = audio_src_ext or ""
            needs_processing = (
                src_ext in ALLOWED_VIDEO_EXT
                or src_ext != ".mp3"
                or t_start is not None
                or t_end is not None
            )
            tmp_src = None
            tmp_dst = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=src_ext, dir=DATA_DIR) as tf:
                    tmp_src = tf.name
                    tf.write(audio_raw)
                if needs_processing:
                    tmp_dst = os.path.join(DATA_DIR, f"out_{secrets.token_hex(8)}.mp3")
                    process_media_to_mp3(tmp_src, tmp_dst, t_start, t_end)
                    with open(tmp_dst, "rb") as f:
                        audio_bytes = f.read()
                else:
                    audio_bytes = audio_raw
                audio_filename = "audio.mp3"
            finally:
                for p in (tmp_src, tmp_dst):
                    if p and os.path.isfile(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
        update_job(job_id, status="processing", progress=40)

        # 2) Insert DB rows + upload objects. Roll back B2 objects if DB fails.
        now = int(time.time())
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scores (name, mode, audio_filename, owner_id, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, mode, audio_filename, owner_id, now),
                )
                score_id = cur.fetchone()[0]
                total = max(len(page_rows), 1)
                for i, (idx, fname, ts, content) in enumerate(page_rows):
                    key = _score_key(score_id, fname)
                    b2_upload_bytes(key, content, _ctype(fname))
                    uploaded_keys.append(key)
                    cur.execute(
                        "INSERT INTO pages (score_id, page_index, image_filename, turn_seconds) VALUES (%s, %s, %s, %s)",
                        (score_id, idx, fname, ts),
                    )
                    update_job(job_id, progress=40 + int(50 * (i + 1) / total))
                if audio_bytes is not None:
                    key = _score_key(score_id, audio_filename)
                    b2_upload_bytes(key, audio_bytes, "audio/mpeg")
                    uploaded_keys.append(key)
        _invalidate_score_cache(score_id)
        update_job(job_id, status="done", progress=100, score_id=score_id)
    except Exception as e:  # noqa: BLE001 - report any failure back to the job row
        for k in uploaded_keys:
            try:
                get_s3().delete_object(Bucket=B2_BUCKET, Key=k)
            except Exception:
                pass
        try:
            update_job(job_id, status="error", error_msg=str(e)[:500])
        except Exception:
            pass


@app.get("/api/scores")
async def list_scores(request: Request):
    me = current_user(request)
    cache_key = ("scores_list", me["id"], me["role"])
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    if me["role"] == "superadmin":
        rows = db_query(
            "SELECT s.id, s.name, s.mode, s.audio_filename, s.created_at, u.username AS owner "
            "FROM scores s LEFT JOIN users u ON s.owner_id = u.id ORDER BY s.created_at DESC"
        )
    else:
        rows = db_query(
            "SELECT s.id, s.name, s.mode, s.audio_filename, s.created_at, %s AS owner "
            "FROM scores s WHERE s.owner_id = %s ORDER BY s.created_at DESC",
            (me["username"], me["id"]),
        )
    result = []
    for r in rows or []:
        cnt = db_query(
            "SELECT COUNT(*) AS c FROM pages WHERE score_id = %s", (r["id"],), one=True
        )
        first_page = db_query(
            "SELECT image_filename FROM pages WHERE score_id = %s ORDER BY page_index LIMIT 1",
            (r["id"],),
            one=True,
        )
        result.append(
            {
                "id": r["id"],
                "name": r["name"],
                "mode": r["mode"],
                "page_count": cnt["c"] if cnt else 0,
                "has_audio": bool(r["audio_filename"]),
                "owner": r.get("owner"),
                "created_at": r["created_at"],
                "created_at_text": _fmt_time(r["created_at"]),
                "cover_url": _media_url(r["id"], first_page["image_filename"]) if first_page else "",
            }
        )
    return _cache_set(cache_key, {"scores": result, "role": me["role"]})


def _safe_ext(filename: str, allowed: set) -> Optional[str]:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in allowed else None


def _safe_zip_name(filename: str, fallback: str) -> str:
    base = posixpath.basename(filename or "").strip()
    base = re.sub(r"[\\/:*?\"<>|]+", "", base).strip()
    return base or fallback


def _content_disposition_attachment(filename: str) -> str:
    quoted = quote(filename.encode("utf-8"))
    return f"attachment; filename*=UTF-8''{quoted}"


def _media_manifest_name(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    return "audio/" + _safe_zip_name(filename, "audio.mp3")


def _media_url(score_id: int, filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    return f"/api/media/{score_id}/{quote(posixpath.basename(filename))}"


@app.get("/api/scores/{score_id}/export")
async def export_score(request: Request, score_id: int):
    me = current_user(request)
    row = db_query(
        "SELECT id, name, mode, audio_filename, owner_id, created_at FROM scores WHERE id = %s",
        (score_id,),
        one=True,
    )
    if not row:
        return JSONResponse(status_code=404, content={"detail": "谱子不存在"})
    if me["role"] != "superadmin" and row["owner_id"] != me["id"]:
        return JSONResponse(status_code=403, content={"detail": "无权导出该谱子"})

    pages = db_query(
        "SELECT page_index, image_filename, turn_seconds FROM pages WHERE score_id = %s ORDER BY page_index",
        (score_id,),
    ) or []
    manifest_pages = []
    zip_buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in pages:
                image_name = _safe_zip_name(p["image_filename"], f"page_{p['page_index']:03d}.png")
                archive_path = f"images/{p['page_index']:03d}_{image_name}"
                zf.writestr(archive_path, b2_read_bytes(_score_key(score_id, p["image_filename"])))
                manifest_pages.append(
                    {
                        "index": p["page_index"],
                        "filename": p["image_filename"],
                        "path": archive_path,
                        "turn_seconds": p["turn_seconds"],
                    }
                )
            audio_path = _media_manifest_name(row["audio_filename"])
            if row["audio_filename"] and audio_path:
                zf.writestr(audio_path, b2_read_bytes(_score_key(score_id, row["audio_filename"])))
            manifest = {
                "version": 1,
                "exported_at": int(time.time()),
                "score": {
                    "name": row["name"],
                    "mode": row["mode"],
                    "audio_filename": row["audio_filename"],
                    "created_at": row["created_at"],
                },
                "pages": manifest_pages,
                "audio": {"path": audio_path, "filename": row["audio_filename"]} if audio_path else None,
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"导出失败: {e}"})

    zip_buffer.seek(0)
    safe_title = _safe_zip_name(row["name"], f"score_{score_id}")
    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition_attachment(f"{safe_title}.zip")},
    )


def _normalize_score_ids(raw_ids) -> List[int]:
    ids: List[int] = []
    seen = set()
    for raw in raw_ids or []:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in seen:
            ids.append(sid)
            seen.add(sid)
    return ids


def _get_accessible_scores(me: dict, ids: List[int]) -> List[dict]:
    if not ids:
        return []
    if me["role"] == "superadmin":
        rows = db_query(
            "SELECT id, name, mode, audio_filename, owner_id, created_at FROM scores WHERE id = ANY(%s) ORDER BY created_at DESC",
            (ids,),
        )
    else:
        rows = db_query(
            "SELECT id, name, mode, audio_filename, owner_id, created_at FROM scores WHERE id = ANY(%s) AND owner_id = %s ORDER BY created_at DESC",
            (ids, me["id"]),
        )
    return rows or []


def _write_score_package(zf: zipfile.ZipFile, row: dict, package_name: str) -> None:
    score_id = row["id"]
    pages = db_query(
        "SELECT page_index, image_filename, turn_seconds FROM pages WHERE score_id = %s ORDER BY page_index",
        (score_id,),
    ) or []
    manifest_pages = []
    for p in pages:
        image_name = _safe_zip_name(p["image_filename"], f"page_{p['page_index']:03d}.png")
        archive_path = f"images/{p['page_index']:03d}_{image_name}"
        zf.writestr(
            f"{package_name}/{archive_path}",
            b2_read_bytes(_score_key(score_id, p["image_filename"])),
        )
        manifest_pages.append(
            {
                "index": p["page_index"],
                "filename": p["image_filename"],
                "path": archive_path,
                "turn_seconds": p["turn_seconds"],
            }
        )
    audio_path = _media_manifest_name(row["audio_filename"])
    if row["audio_filename"] and audio_path:
        zf.writestr(
            f"{package_name}/{audio_path}",
            b2_read_bytes(_score_key(score_id, row["audio_filename"])),
        )
    manifest = {
        "version": 1,
        "exported_at": int(time.time()),
        "score": {
            "name": row["name"],
            "mode": row["mode"],
            "audio_filename": row["audio_filename"],
            "created_at": row["created_at"],
        },
        "pages": manifest_pages,
        "audio": {"path": audio_path, "filename": row["audio_filename"]} if audio_path else None,
    }
    zf.writestr(f"{package_name}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))


@app.post("/api/scores/batch-export")
async def batch_export_scores(request: Request):
    me = current_user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = _normalize_score_ids(body.get("ids"))
    if not ids:
        return JSONResponse(status_code=400, content={"detail": "请选择要导出的谱子"})
    rows = _get_accessible_scores(me, ids)
    if len(rows) != len(ids):
        return JSONResponse(status_code=403, content={"detail": "部分谱子不存在或无权导出"})

    zip_buffer = io.BytesIO()
    used_names = set()
    try:
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for row in rows:
                base_name = _safe_zip_name(row["name"], f"score_{row['id']}")
                package_name = base_name
                idx = 2
                while package_name in used_names:
                    package_name = f"{base_name}({idx})"
                    idx += 1
                used_names.add(package_name)
                _write_score_package(zf, row, package_name)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"批量导出失败: {e}"})

    zip_buffer.seek(0)
    export_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    export_filename = f"谱子导出_{export_time}.zip"
    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition_attachment(export_filename)},
    )


@app.post("/api/scores/batch-delete")
async def batch_delete_scores(request: Request):
    me = current_user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = _normalize_score_ids(body.get("ids"))
    if not ids:
        return JSONResponse(status_code=400, content={"detail": "请选择要删除的谱子"})
    rows = _get_accessible_scores(me, ids)
    if len(rows) != len(ids):
        return JSONResponse(status_code=403, content={"detail": "部分谱子不存在或无权删除"})

    prefixes = [f"scores/{score_id}/" for score_id in ids]
    keys_to_delete: List[str] = []
    for prefix in prefixes:
        keys_to_delete.extend(b2_list_keys(prefix))
    b2_delete_keys(keys_to_delete)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scores WHERE id = ANY(%s)", (ids,))
    _invalidate_score_cache()
    return {"ok": True, "deleted": len(ids), "b2_deleted": len(keys_to_delete)}


@app.post("/api/scores/import")
async def import_score(request: Request, package: UploadFile = File(...)):
    me = current_user(request)
    if not package or not package.filename:
        return JSONResponse(status_code=400, content={"detail": "请上传导出的 ZIP 文件"})
    if os.path.splitext(package.filename)[1].lower() != ".zip":
        return JSONResponse(status_code=400, content={"detail": "仅支持导入 ZIP 文件"})

    uploaded_keys: List[str] = []
    try:
        raw = await package.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                return JSONResponse(status_code=400, content={"detail": "ZIP 中缺少 manifest.json"})
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            score_meta = manifest.get("score") or {}
            name = (score_meta.get("name") or "导入谱子").strip()
            mode = score_meta.get("mode") or "audio"
            pages_meta = manifest.get("pages") or []
            audio_meta = manifest.get("audio") or None
            if mode not in ("audio", "countdown"):
                return JSONResponse(status_code=400, content={"detail": "manifest 中的翻页模式无效"})
            if not pages_meta:
                return JSONResponse(status_code=400, content={"detail": "manifest 中缺少谱页信息"})
            if mode == "audio" and not (audio_meta and audio_meta.get("path")):
                return JSONResponse(status_code=400, content={"detail": "跟伴奏模式缺少伴奏文件"})

            page_rows = []
            for idx, p in enumerate(sorted(pages_meta, key=lambda x: int(x.get("index", 0)))):
                archive_path = p.get("path")
                if not archive_path or archive_path not in names:
                    return JSONResponse(status_code=400, content={"detail": f"缺少谱页文件: {archive_path or idx}"})
                original_name = p.get("filename") or posixpath.basename(archive_path)
                ext = _safe_ext(original_name, ALLOWED_IMAGE_EXT) or _safe_ext(archive_path, ALLOWED_IMAGE_EXT) or ".png"
                fname = f"page_{idx:03d}{ext}"
                try:
                    turn = float(p.get("turn_seconds") or 0)
                except (TypeError, ValueError):
                    turn = 0.0
                if turn < 0:
                    turn = 0.0
                page_rows.append((idx, fname, turn, zf.read(archive_path)))

            audio_filename = None
            audio_bytes = None
            if audio_meta and audio_meta.get("path"):
                audio_path = audio_meta.get("path")
                if audio_path not in names:
                    return JSONResponse(status_code=400, content={"detail": "ZIP 中缺少伴奏文件"})
                original_audio_name = audio_meta.get("filename") or posixpath.basename(audio_path)
                src_ext = os.path.splitext(original_audio_name or audio_path)[1].lower()
                if src_ext not in ALLOWED_AUDIO_EXT and src_ext not in ALLOWED_VIDEO_EXT:
                    return JSONResponse(status_code=400, content={"detail": f"不支持的伴奏文件格式: {src_ext or '未知'}"})
                audio_filename = "audio.mp3" if src_ext != ".mp3" else "audio.mp3"
                audio_bytes = zf.read(audio_path)

        now = int(time.time())
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scores (name, mode, audio_filename, owner_id, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, mode, audio_filename, me["id"], now),
                )
                score_id = cur.fetchone()[0]
                for idx, fname, turn, content in page_rows:
                    key = _score_key(score_id, fname)
                    b2_upload_bytes(key, content, _ctype(fname))
                    uploaded_keys.append(key)
                    cur.execute(
                        "INSERT INTO pages (score_id, page_index, image_filename, turn_seconds) VALUES (%s, %s, %s, %s)",
                        (score_id, idx, fname, turn),
                    )
                if audio_bytes is not None:
                    key = _score_key(score_id, audio_filename)
                    b2_upload_bytes(key, audio_bytes, "audio/mpeg")
                    uploaded_keys.append(key)
        _invalidate_score_cache(score_id)
        return {"ok": True, "id": score_id}
    except zipfile.BadZipFile:
        return JSONResponse(status_code=400, content={"detail": "ZIP 文件无法解析"})
    except Exception as e:
        for k in uploaded_keys:
            try:
                get_s3().delete_object(Bucket=B2_BUCKET, Key=k)
            except Exception:
                pass
        return JSONResponse(status_code=500, content={"detail": f"导入失败: {e}"})
AUTO_CROP_LIGHTNESS_THRESHOLD = int(os.environ.get("AUTO_CROP_LIGHTNESS_THRESHOLD", "245"))
AUTO_CROP_ALPHA_THRESHOLD = int(os.environ.get("AUTO_CROP_ALPHA_THRESHOLD", "10"))


def detect_light_margin_crop_box(data: bytes) -> dict:
    """Return a crop box that removes white/light margins from an image.

    The detection treats pixels darker than AUTO_CROP_LIGHTNESS_THRESHOLD as content.
    Transparent pixels are ignored as background. The returned box uses Pillow/CSS
    image coordinates: left/top inclusive, right/bottom exclusive.
    """
    with Image.open(BytesIO(data)) as img:
        rgba = img.convert("RGBA")
        width, height = rgba.size
        pix = rgba.load()
        min_x, min_y = width, height
        max_x, max_y = -1, -1
        threshold = AUTO_CROP_LIGHTNESS_THRESHOLD
        alpha_threshold = AUTO_CROP_ALPHA_THRESHOLD

        for y in range(height):
            for x in range(width):
                r, g, b, a = pix[x, y]
                if a <= alpha_threshold:
                    continue
                # Perceived brightness; lower means darker/non-margin content.
                brightness = (r * 299 + g * 587 + b * 114) / 1000
                if brightness < threshold:
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y

        if max_x < min_x or max_y < min_y:
            return {"left": 0, "top": 0, "right": width, "bottom": height, "width": width, "height": height}

        return {
            "left": min_x,
            "top": min_y,
            "right": max_x + 1,
            "bottom": max_y + 1,
            "width": max_x - min_x + 1,
            "height": max_y - min_y + 1,
        }


_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


@app.post("/api/images/auto-crop")
async def api_auto_crop_image(image: UploadFile = File(...)):
    # 先尝试从文件名拿扩展名，失败时从 Content-Type 兜底（Android WebView
    # 通过 file:// URI 选图时 filename 可能为空或无扩展名）
    ext = _safe_ext(image.filename, ALLOWED_IMAGE_EXT)
    if not ext:
        ct = (image.content_type or "").split(";")[0].strip().lower()
        ext = _MIME_TO_EXT.get(ct)
    if not ext:
        print(
            f"[auto-crop] rejected: filename={image.filename!r}, "
            f"content_type={image.content_type!r}"
        )
        return JSONResponse(status_code=400, content={"detail": "不支持的图片文件格式"})
    try:
        content = await image.read()
        if not content:
            return JSONResponse(
                status_code=400,
                content={"detail": "图片内容为空，可能是跨域上传问题"},
            )
        with Image.open(BytesIO(content)) as img:
            image_size = {"width": img.width, "height": img.height}
        return {"ok": True, "box": detect_light_margin_crop_box(content), "image": image_size}
    except Exception as e:
        import traceback

        print(
            f"[auto-crop error] filename={image.filename!r}, "
            f"content_type={image.content_type!r}, "
            f"content_len={len(content) if 'content' in dir() else 'N/A'}, error={e}"
        )
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"detail": f"图片自动裁剪失败: {e}"})


@app.post("/api/scores")
async def create_score(
    request: Request,
    name: str = Form(...),
    mode: str = Form(...),
    turn_seconds: List[str] = Form(default=[]),
    trim_start: Optional[str] = Form(default=None),
    trim_end: Optional[str] = Form(default=None),
    images: List[UploadFile] = File(...),
    audio: Optional[UploadFile] = File(default=None),
):
    me = current_user(request)
    name = (name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "谱子名称不能为空"})
    if mode not in ("audio", "countdown"):
        return JSONResponse(status_code=400, content={"detail": "翻页模式无效"})
    if not images:
        return JSONResponse(status_code=400, content={"detail": "请至少上传一张谱页图片"})
    if mode == "audio" and not (audio and audio.filename):
        return JSONResponse(
            status_code=400, content={"detail": "跟伴奏模式必须上传音频或视频文件"}
        )

    def _parse_float(v):
        try:
            f = float(v)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    t_start = _parse_float(trim_start)
    t_end = _parse_float(trim_end)
    if t_start is not None and t_end is not None and t_end <= t_start:
        t_start, t_end = None, None

    now = int(time.time())

    # 1) Read + upload images (collect for possible rollback of B2 objects)
    uploaded_keys: List[str] = []
    page_rows = []  # (idx, fname, ts)
    for idx, img in enumerate(images):
        ext = _safe_ext(img.filename, ALLOWED_IMAGE_EXT) or ".png"
        fname = f"page_{idx:03d}{ext}"
        content = await img.read()
        try:
            ts = float(turn_seconds[idx]) if idx < len(turn_seconds) else 0.0
        except (ValueError, TypeError):
            ts = 0.0
        if ts < 0:
            ts = 0.0
        page_rows.append((idx, fname, ts, content))

    # 2) Process audio/video -> mp3 bytes (if provided)
    audio_filename = None
    audio_bytes = None
    if audio and audio.filename:
        src_ext = os.path.splitext(audio.filename)[1].lower()
        if src_ext not in ALLOWED_AUDIO_EXT and src_ext not in ALLOWED_VIDEO_EXT:
            return JSONResponse(
                status_code=400,
                content={"detail": f"不支持的伴奏文件格式: {src_ext or '未知'}"},
            )
        needs_processing = (
            src_ext in ALLOWED_VIDEO_EXT
            or src_ext != ".mp3"
            or t_start is not None
            or t_end is not None
        )
        tmp_src = None
        tmp_dst = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=src_ext, dir=DATA_DIR) as tf:
                tmp_src = tf.name
                tf.write(await audio.read())
            if needs_processing:
                tmp_dst = os.path.join(DATA_DIR, f"out_{secrets.token_hex(8)}.mp3")
                process_media_to_mp3(tmp_src, tmp_dst, t_start, t_end)
                with open(tmp_dst, "rb") as f:
                    audio_bytes = f.read()
                audio_filename = "audio.mp3"
            else:
                with open(tmp_src, "rb") as f:
                    audio_bytes = f.read()
                audio_filename = "audio.mp3"
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"伴奏处理失败: {e}"})
        finally:
            for p in (tmp_src, tmp_dst):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # 3) Insert DB rows + upload objects. Roll back B2 objects if DB fails.
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scores (name, mode, audio_filename, owner_id, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, mode, audio_filename, me["id"], now),
                )
                score_id = cur.fetchone()[0]
                for idx, fname, ts, content in page_rows:
                    key = _score_key(score_id, fname)
                    b2_upload_bytes(key, content, _ctype(fname))
                    uploaded_keys.append(key)
                    cur.execute(
                        "INSERT INTO pages (score_id, page_index, image_filename, turn_seconds) VALUES (%s, %s, %s, %s)",
                        (score_id, idx, fname, ts),
                    )
                if audio_bytes is not None:
                    key = _score_key(score_id, audio_filename)
                    b2_upload_bytes(key, audio_bytes, "audio/mpeg")
                    uploaded_keys.append(key)
        _invalidate_score_cache(score_id)
        return {"ok": True, "id": score_id}
    except Exception as e:
        # best-effort cleanup of any uploaded objects
        for k in uploaded_keys:
            try:
                get_s3().delete_object(Bucket=B2_BUCKET, Key=k)
            except Exception:
                pass
        return JSONResponse(status_code=500, content={"detail": f"保存失败: {e}"})


@app.post("/api/scores/async")
async def api_create_score_async(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    mode: str = Form(...),
    turn_seconds: List[str] = Form(default=[]),
    trim_start: Optional[str] = Form(default=None),
    trim_end: Optional[str] = Form(default=None),
    images: List[UploadFile] = File(...),
    audio: Optional[UploadFile] = File(default=None),
):
    """Accept a new-score request, persist a pending job, and process it in the
    background. Returns immediately so the frontend is never blocked."""
    me = current_user(request)
    name = (name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "谱子名称不能为空"})
    if mode not in ("audio", "countdown"):
        return JSONResponse(status_code=400, content={"detail": "翻页模式无效"})
    if not images:
        return JSONResponse(status_code=400, content={"detail": "请至少上传一张谱页图片"})
    if mode == "audio" and not (audio and audio.filename):
        return JSONResponse(
            status_code=400, content={"detail": "跟伴奏模式必须上传音频或视频文件"}
        )

    def _parse_float(v):
        try:
            f = float(v)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    t_start = _parse_float(trim_start)
    t_end = _parse_float(trim_end)
    if t_start is not None and t_end is not None and t_end <= t_start:
        t_start, t_end = None, None

    # Read ALL uploaded bytes now: UploadFile objects are closed once this
    # request returns, but the background task runs afterwards.
    page_rows = []  # (idx, fname, ts, content)
    for idx, img in enumerate(images):
        ext = _safe_ext(img.filename, ALLOWED_IMAGE_EXT) or ".png"
        fname = f"page_{idx:03d}{ext}"
        content = await img.read()
        try:
            ts = float(turn_seconds[idx]) if idx < len(turn_seconds) else 0.0
        except (ValueError, TypeError):
            ts = 0.0
        if ts < 0:
            ts = 0.0
        page_rows.append((idx, fname, ts, content))

    audio_raw = None
    audio_src_ext = None
    if audio and audio.filename:
        audio_src_ext = os.path.splitext(audio.filename)[1].lower()
        if audio_src_ext not in ALLOWED_AUDIO_EXT and audio_src_ext not in ALLOWED_VIDEO_EXT:
            return JSONResponse(
                status_code=400,
                content={"detail": f"不支持的伴奏文件格式: {audio_src_ext or '未知'}"},
            )
        audio_raw = await audio.read()

    job_id = create_job(me["id"], name)
    background_tasks.add_task(
        process_score_job,
        job_id,
        me["id"],
        name,
        mode,
        page_rows,
        audio_raw,
        audio_src_ext,
        t_start,
        t_end,
    )
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/scores/jobs")
async def list_score_jobs(request: Request, limit: int = 20):
    """Return the current user's most recent score-creation jobs."""
    me = current_user(request)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    rows = db_query(
        "SELECT id, title, status, progress, score_id, error_msg, created_at, updated_at "
        "FROM score_jobs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
        (me["id"], limit),
    )
    return {"jobs": [_job_to_dict(r) for r in (rows or [])]}


@app.get("/api/scores/jobs/{job_id}")
async def get_score_job(request: Request, job_id: str):
    """Return the detailed state of a single job owned by the current user."""
    me = current_user(request)
    row = db_query(
        "SELECT id, title, status, progress, score_id, error_msg, created_at, updated_at "
        "FROM score_jobs WHERE id = %s AND user_id = %s",
        (job_id, me["id"]),
        one=True,
    )
    if not row:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    return _job_to_dict(row)


@app.get("/api/scores/{score_id}")
async def get_score(request: Request, score_id: int):
    me = current_user(request)
    cache_key = ("score_detail", score_id, me["id"], me["role"])
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    row = db_query(
        "SELECT id, name, mode, audio_filename, owner_id, created_at FROM scores WHERE id = %s",
        (score_id,),
        one=True,
    )
    if not row:
        return JSONResponse(status_code=404, content={"detail": "谱子不存在"})
    if me["role"] != "superadmin" and row["owner_id"] != me["id"]:
        return JSONResponse(status_code=403, content={"detail": "无权访问该谱子"})
    pages = db_query(
        "SELECT page_index, image_filename, turn_seconds FROM pages WHERE score_id = %s ORDER BY page_index",
        (score_id,),
    )
    result = {
        "id": row["id"],
        "name": row["name"],
        "mode": row["mode"],
        "created_at": row["created_at"],
        "created_at_text": _fmt_time(row["created_at"]),
        "can_edit": me["role"] == "superadmin" or row["owner_id"] == me["id"],
        "audio_filename": row["audio_filename"],
        "audio_url": _media_url(score_id, row["audio_filename"]),
        "pages": [
            {
                "index": p["page_index"],
                "filename": p["image_filename"],
                "image_url": _media_url(score_id, p["image_filename"]),
                "turn_seconds": p["turn_seconds"],
            }
            for p in (pages or [])
        ],
    }
    return _cache_set(cache_key, result)


@app.delete("/api/scores/{score_id}")
async def delete_score(request: Request, score_id: int):
    me = current_user(request)
    row = db_query(
        "SELECT id, owner_id FROM scores WHERE id = %s", (score_id,), one=True
    )
    if not row:
        return JSONResponse(status_code=404, content={"detail": "谱子不存在"})
    if me["role"] != "superadmin" and row["owner_id"] != me["id"]:
        return JSONResponse(status_code=403, content={"detail": "无权删除该谱子"})
    keys_to_delete = b2_list_keys(f"scores/{score_id}/")
    b2_delete_keys(keys_to_delete)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scores WHERE id = %s", (score_id,))
    _invalidate_score_cache(score_id)
    return {"ok": True, "b2_deleted": len(keys_to_delete)}


# ----------------------------------------------------------------------------
# Update / edit an existing score (owner or superadmin)
# ----------------------------------------------------------------------------
@app.post("/api/scores/{score_id}/update")
async def update_score(
    request: Request,
    score_id: int,
    name: str = Form(...),
    mode: str = Form(...),
    pages_meta: str = Form(...),
    audio_action: str = Form("keep"),  # keep | replace | remove
    trim_start: Optional[str] = Form(default=None),
    trim_end: Optional[str] = Form(default=None),
    images: List[UploadFile] = File(default=[]),  # new files, in order of 'new' entries
    audio: Optional[UploadFile] = File(default=None),
):
    me = current_user(request)
    row = db_query(
        "SELECT id, mode, audio_filename, owner_id FROM scores WHERE id = %s",
        (score_id,),
        one=True,
    )
    if not row:
        return JSONResponse(status_code=404, content={"detail": "谱子不存在"})
    if me["role"] != "superadmin" and row["owner_id"] != me["id"]:
        return JSONResponse(status_code=403, content={"detail": "无权编辑该谱子"})

    name = (name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "谱子名称不能为空"})
    if mode not in ("audio", "countdown"):
        return JSONResponse(status_code=400, content={"detail": "翻页模式无效"})
    try:
        meta = json.loads(pages_meta)
        assert isinstance(meta, list) and meta
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "页面数据无效"})

    existing = db_query(
        "SELECT image_filename FROM pages WHERE score_id = %s", (score_id,)
    ) or []
    existing_files = {p["image_filename"] for p in existing}

    def _pf(v):
        try:
            f = float(v)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    # Build the new ordered page list; upload new images with unique keys.
    new_uploaded_keys: List[str] = []
    final_pages = []  # (image_filename, turn_seconds)
    new_iter = iter(images or [])
    try:
        for item in meta:
            turn = item.get("turn", 0) or 0
            try:
                turn = float(turn)
            except (TypeError, ValueError):
                turn = 0.0
            if turn < 0:
                turn = 0.0
            kind = item.get("kind")
            if kind == "existing":
                fn = item.get("filename")
                if fn in existing_files:
                    final_pages.append((fn, turn))
            elif kind == "new":
                up = next(new_iter, None)
                if up is None or not up.filename:
                    continue
                ext = _safe_ext(up.filename, ALLOWED_IMAGE_EXT) or ".png"
                fn = f"p_{secrets.token_hex(8)}{ext}"
                content = await up.read()
                key = _score_key(score_id, fn)
                b2_upload_bytes(key, content, _ctype(fn))
                new_uploaded_keys.append(key)
                final_pages.append((fn, turn))
        if not final_pages:
            raise ValueError("至少需要保留或上传一张谱页图片")

        # Audio handling
        t_start = _pf(trim_start)
        t_end = _pf(trim_end)
        if t_start is not None and t_end is not None and t_end <= t_start:
            t_start, t_end = None, None

        audio_filename = row["audio_filename"]
        replaced_audio = False
        if audio_action == "remove":
            audio_filename = None
        elif audio_action == "replace":
            if not (audio and audio.filename):
                raise ValueError("选择替换音频但未提供文件")
            src_ext = os.path.splitext(audio.filename)[1].lower()
            if src_ext not in ALLOWED_AUDIO_EXT and src_ext not in ALLOWED_VIDEO_EXT:
                raise ValueError(f"不支持的伴奏文件格式: {src_ext or '未知'}")
            needs = (
                src_ext in ALLOWED_VIDEO_EXT
                or src_ext != ".mp3"
                or t_start is not None
                or t_end is not None
            )
            tmp_src = tmp_dst = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=src_ext, dir=DATA_DIR) as tf:
                    tmp_src = tf.name
                    tf.write(await audio.read())
                if needs:
                    tmp_dst = os.path.join(DATA_DIR, f"out_{secrets.token_hex(8)}.mp3")
                    process_media_to_mp3(tmp_src, tmp_dst, t_start, t_end)
                    with open(tmp_dst, "rb") as f:
                        abytes = f.read()
                else:
                    with open(tmp_src, "rb") as f:
                        abytes = f.read()
            finally:
                for p in (tmp_src, tmp_dst):
                    if p and os.path.isfile(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
            new_audio_filename = f"audio_{secrets.token_hex(8)}.mp3"
            new_audio_key = _score_key(score_id, new_audio_filename)
            b2_upload_bytes(new_audio_key, abytes, "audio/mpeg")
            new_uploaded_keys.append(new_audio_key)
            audio_filename = new_audio_filename
            replaced_audio = True

        if mode == "audio" and not audio_filename:
            raise ValueError("跟伴奏模式必须有音频伴奏")

        # Persist: rewrite pages + update score in one transaction
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pages WHERE score_id = %s", (score_id,))
                for idx, (fn, ts) in enumerate(final_pages):
                    cur.execute(
                        "INSERT INTO pages (score_id, page_index, image_filename, turn_seconds) VALUES (%s, %s, %s, %s)",
                        (score_id, idx, fn, ts),
                    )
                cur.execute(
                    "UPDATE scores SET name = %s, mode = %s, audio_filename = %s WHERE id = %s",
                    (name, mode, audio_filename, score_id),
                )
    except Exception as e:
        for k in new_uploaded_keys:  # rollback freshly-uploaded images
            try:
                get_s3().delete_object(Bucket=B2_BUCKET, Key=k)
            except Exception:
                pass
        return JSONResponse(status_code=400, content={"detail": f"更新失败: {e}"})

    # After successful commit: purge removed image objects (best-effort)
    kept = {fn for fn, _ in final_pages}
    for fn in existing_files - kept:
        try:
            get_s3().delete_object(Bucket=B2_BUCKET, Key=_score_key(score_id, fn))
        except Exception:
            pass
    old_audio_filename = row["audio_filename"]
    if old_audio_filename and (audio_action == "remove" or replaced_audio):
        try:
            b2_delete_key(_score_key(score_id, old_audio_filename))
        except Exception:
            pass
    _invalidate_score_cache(score_id)
    return {"ok": True, "id": score_id}


@app.post("/api/admin/b2/cleanup-orphans")
async def cleanup_b2_orphans(request: Request):
    if not is_superadmin(request):
        return JSONResponse(status_code=403, content={"detail": "需要超级管理员权限"})
    dry_run = (request.query_params.get("dry_run", "false").lower() in ("1", "true", "yes", "on"))

    rows = db_query("SELECT id, audio_filename FROM scores") or []
    pages = db_query("SELECT score_id, image_filename FROM pages") or []
    referenced_keys = set()
    for row in rows:
        if row.get("audio_filename"):
            referenced_keys.add(_score_key(row["id"], row["audio_filename"]))
    for page in pages:
        if page.get("image_filename"):
            referenced_keys.add(_score_key(page["score_id"], page["image_filename"]))

    b2_keys = set(b2_list_keys("scores/"))
    orphan_keys = sorted(b2_keys - referenced_keys)
    if not dry_run:
        b2_delete_keys(orphan_keys)
    return {
        "ok": True,
        "dry_run": dry_run,
        "scanned": len(b2_keys),
        "referenced": len(referenced_keys),
        "orphans": len(orphan_keys),
        "deleted": 0 if dry_run else len(orphan_keys),
        "keys": orphan_keys[:200],
    }


# ----------------------------------------------------------------------------
# Media redirect (backward-compat): 302 -> presigned B2 URL
# ----------------------------------------------------------------------------
@app.get("/api/media/{score_id}/{filename}")
async def serve_media(score_id: int, filename: str):
    if "/" in filename or ".." in filename:
        return Response(status_code=400)
    url = b2_presigned_url(_score_key(score_id, filename))
    return RedirectResponse(url=url, status_code=302)


# Initialise database on import
try:
    init_db()
except Exception as _e:
    # Defer failures to first request so the process can still boot for /ping.
    print(f"[init_db] warning: {_e}")


# ---------------------------DO NOT EDIT CODE BELOW THIS LINE---------------------------------
# This is the entry point for the FastAPI application.
if __name__ == "__main__":
    port = int(os.environ.get("_BYTEFAAS_RUNTIME_PORT", 8000))
    config = uvicorn.Config("main:app", port=port, log_level="info", host=None)
    server = uvicorn.Server(config)
    server.run()
# --------------------------------------------------------------------------------------------
