import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone, timedelta

import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import (
    JSONResponse,
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    Response,
    StreamingResponse,
)
from typing import List, Optional

# ----------------------------------------------------------------------------
# Paths & configuration
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
# Runtime data must live on a writable path (the app bundle is read-only at runtime).
DATA_DIR = os.environ.get("SCORE_DATA_DIR", "/tmp/score_app_data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "scores.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)

# Fixed admin credentials. Only the PBKDF2 hash is stored (encrypted at rest).
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_SALT = os.environ.get(
    "ADMIN_SALT", "151ef02104f1cd54a042d2295e9929b8"
)
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

    cmd = [ffmpeg, "-y"]
    # Accurate trimming: place -ss/-to after -i.
    cmd += ["-i", src_path]
    if trim_start is not None and trim_start > 0:
        cmd += ["-ss", f"{trim_start:.3f}"]
    if trim_end is not None and trim_end > 0:
        cmd += ["-to", f"{trim_end:.3f}"]
    # -vn: drop any video stream; encode audio to mp3.
    cmd += ["-vn", "-acodec", "libmp3lame", "-q:a", "2", dest_path]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=600,
    )
    if proc.returncode != 0 or not os.path.isfile(dest_path):
        tail = proc.stderr.decode("utf-8", "ignore")[-800:]
        raise RuntimeError(f"ffmpeg 处理失败: {tail}")


# ----------------------------------------------------------------------------
# Database helpers
# ----------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                audio_filename TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                score_id INTEGER NOT NULL,
                page_index INTEGER NOT NULL,
                image_filename TEXT NOT NULL,
                turn_seconds REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (score_id) REFERENCES scores(id) ON DELETE CASCADE
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_sessions() -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------
def verify_password(password: str) -> bool:
    try:
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(ADMIN_SALT), PBKDF2_ROUNDS
        ).hex()
    except Exception:
        return False
    return secrets.compare_digest(computed, ADMIN_HASH)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO sessions (token, created_at, expires_at) VALUES (?, ?, ?)",
            (token, now, now + SESSION_TTL_SECONDS),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    return int(row["expires_at"]) >= int(time.time())


def destroy_session(token: Optional[str]) -> None:
    if not token:
        return
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# App & middleware
# ----------------------------------------------------------------------------
app = FastAPI(
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# Paths that never require an authenticated session.
PUBLIC_EXACT = {"/login", "/api/login", "/api", "/api/v1/ping", "/favicon.ico"}
PUBLIC_PREFIX = ("/assets/", "/api/docs", "/api/redoc", "/api/openapi.json")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path

    if path in PUBLIC_EXACT or any(path.startswith(p) for p in PUBLIC_PREFIX):
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE)
    if session_valid(token):
        return await call_next(request)

    # Not authenticated
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
    # Page routes -> redirect to login
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


@app.get("/assets/{filename:path}")
async def assets(filename: str):
    return serve_static_file(filename)


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

    if username != ADMIN_USERNAME or not verify_password(password):
        return JSONResponse(status_code=401, content={"detail": "用户名或密码错误"})

    cleanup_sessions()
    token = create_session()
    resp = JSONResponse(content={"ok": True})
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
    token = request.cookies.get(SESSION_COOKIE)
    destroy_session(token)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/session")
async def api_session():
    # If the request reached here, auth_gate already validated the session.
    return {"authenticated": True, "username": ADMIN_USERNAME}


# ----------------------------------------------------------------------------
# Scores API
# ----------------------------------------------------------------------------
def _fmt_time(ts: int) -> str:
    # Display in China Standard Time (UTC+8)
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M")


@app.get("/api/scores")
async def list_scores():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, mode, audio_filename, created_at FROM scores ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            page_count = conn.execute(
                "SELECT COUNT(*) AS c FROM pages WHERE score_id = ?", (r["id"],)
            ).fetchone()["c"]
            result.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "mode": r["mode"],
                    "page_count": page_count,
                    "has_audio": bool(r["audio_filename"]),
                    "created_at": r["created_at"],
                    "created_at_text": _fmt_time(r["created_at"]),
                }
            )
        return {"scores": result}
    finally:
        conn.close()


def _safe_ext(filename: str, allowed: set) -> Optional[str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in allowed:
        return ext
    return None


def _rmtree(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


@app.post("/api/scores")
async def create_score(
    name: str = Form(...),
    mode: str = Form(...),
    turn_seconds: List[str] = Form(default=[]),
    trim_start: Optional[str] = Form(default=None),
    trim_end: Optional[str] = Form(default=None),
    images: List[UploadFile] = File(...),
    audio: Optional[UploadFile] = File(default=None),
):
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

    # Parse trim range
    def _parse_float(v):
        try:
            f = float(v)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    t_start = _parse_float(trim_start)
    t_end = _parse_float(trim_end)
    if t_start is not None and t_end is not None and t_end <= t_start:
        t_start, t_end = None, None  # invalid range -> ignore trimming

    now = int(time.time())
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO scores (name, mode, audio_filename, created_at) VALUES (?, ?, ?, ?)",
            (name, mode, None, now),
        )
        score_id = cur.lastrowid
        score_dir = os.path.join(UPLOAD_DIR, str(score_id))
        os.makedirs(score_dir, exist_ok=True)

        # Save images
        for idx, img in enumerate(images):
            ext = _safe_ext(img.filename, ALLOWED_IMAGE_EXT) or ".png"
            fname = f"page_{idx:03d}{ext}"
            dest = os.path.join(score_dir, fname)
            content = await img.read()
            with open(dest, "wb") as f:
                f.write(content)
            try:
                ts = float(turn_seconds[idx]) if idx < len(turn_seconds) else 0.0
            except (ValueError, TypeError):
                ts = 0.0
            if ts < 0:
                ts = 0.0
            conn.execute(
                "INSERT INTO pages (score_id, page_index, image_filename, turn_seconds) VALUES (?, ?, ?, ?)",
                (score_id, idx, fname, ts),
            )

        # Save & process audio/video -> mp3 (extract track from video, trim if requested)
        audio_filename = None
        if audio and audio.filename:
            src_ext = os.path.splitext(audio.filename)[1].lower()
            is_media = src_ext in ALLOWED_AUDIO_EXT or src_ext in ALLOWED_VIDEO_EXT
            if not is_media:
                conn.rollback()
                _rmtree(score_dir)
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
            try:
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=src_ext, dir=DATA_DIR
                ) as tf:
                    tmp_src = tf.name
                    tf.write(await audio.read())

                audio_filename = "audio.mp3"
                dest = os.path.join(score_dir, audio_filename)
                if needs_processing:
                    process_media_to_mp3(tmp_src, dest, t_start, t_end)
                else:
                    shutil.move(tmp_src, dest)
                    tmp_src = None
            except Exception as e:
                conn.rollback()
                _rmtree(score_dir)
                if tmp_src and os.path.isfile(tmp_src):
                    os.remove(tmp_src)
                return JSONResponse(
                    status_code=500, content={"detail": f"伴奏处理失败: {e}"}
                )
            finally:
                if tmp_src and os.path.isfile(tmp_src):
                    os.remove(tmp_src)

            conn.execute(
                "UPDATE scores SET audio_filename = ? WHERE id = ?",
                (audio_filename, score_id),
            )

        conn.commit()
        return {"ok": True, "id": score_id}
    finally:
        conn.close()


@app.get("/api/scores/{score_id}")
async def get_score(score_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, mode, audio_filename, created_at FROM scores WHERE id = ?",
            (score_id,),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"detail": "谱子不存在"})
        pages = conn.execute(
            "SELECT page_index, image_filename, turn_seconds FROM pages WHERE score_id = ? ORDER BY page_index",
            (score_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "name": row["name"],
            "mode": row["mode"],
            "created_at": row["created_at"],
            "created_at_text": _fmt_time(row["created_at"]),
            "audio_url": (
                f"/api/media/{score_id}/{row['audio_filename']}"
                if row["audio_filename"]
                else None
            ),
            "pages": [
                {
                    "index": p["page_index"],
                    "image_url": f"/api/media/{score_id}/{p['image_filename']}",
                    "turn_seconds": p["turn_seconds"],
                }
                for p in pages
            ],
        }
    finally:
        conn.close()


@app.delete("/api/scores/{score_id}")
async def delete_score(score_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM scores WHERE id = ?", (score_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"detail": "谱子不存在"})
        conn.execute("DELETE FROM scores WHERE id = ?", (score_id,))
        conn.commit()
    finally:
        conn.close()
    # Remove files
    score_dir = os.path.join(UPLOAD_DIR, str(score_id))
    if os.path.isdir(score_dir):
        for fn in os.listdir(score_dir):
            try:
                os.remove(os.path.join(score_dir, fn))
            except OSError:
                pass
        try:
            os.rmdir(score_dir)
        except OSError:
            pass
    return {"ok": True}


# ----------------------------------------------------------------------------
# Media serving with HTTP Range support (needed for audio seeking)
# ----------------------------------------------------------------------------
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@app.get("/api/media/{score_id}/{filename}")
async def serve_media(score_id: int, filename: str, request: Request):
    # Prevent path traversal
    if "/" in filename or ".." in filename:
        return Response(status_code=400)
    full = os.path.join(UPLOAD_DIR, str(score_id), filename)
    if not os.path.isfile(full):
        return Response(status_code=404, content="Not Found")

    file_size = os.path.getsize(full)
    ctype, _ = mimetypes.guess_type(full)
    ctype = ctype or "application/octet-stream"

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(full, media_type=ctype)

    m = RANGE_RE.match(range_header)
    if not m:
        return FileResponse(full, media_type=ctype)

    start_s, end_s = m.group(1), m.group(2)
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else file_size - 1
    if start > end or start >= file_size:
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{file_size}"}
        )
    end = min(end, file_size - 1)
    length = end - start + 1

    def iterfile():
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            chunk = 64 * 1024
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": ctype,
    }
    return StreamingResponse(iterfile(), status_code=206, headers=headers)


# Initialise database on import
init_db()


# ---------------------------DO NOT EDIT CODE BELOW THIS LINE---------------------------------
# This is the entry point for the FastAPI application.
if __name__ == "__main__":
    port = int(os.environ.get("_BYTEFAAS_RUNTIME_PORT", 8000))
    config = uvicorn.Config("main:app", port=port, log_level="info", host=None)
    server = uvicorn.Server(config)
    server.run()
# --------------------------------------------------------------------------------------------
