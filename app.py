import os
import re
import secrets
import sqlite3
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask, abort, g, jsonify, render_template,
    request, send_from_directory, session
)
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import psycopg2
    import psycopg2.extras
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
DATA.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)

# ==============================================================================
# VERİTABANI BAĞLANTISI (İstediğinde connection string'ini buraya yazabilirsin)
# Boş bırakırsan ("") -> Mevcut yerel SQLite (data/social.db) çalışır.
# Başında postgresql:// veya postgres:// varsa -> Otomatik algılayıp PostgreSQL'e bağlanır.
# Örnek: DATABASE_URL = "postgresql://kullanici:sifre@ep-xyz.neon.tech/neondb?sslmode=require"
# ==============================================================================
DATABASE_URL = ""


def is_postgres():
    url = (DATABASE_URL or "").strip()
    return url.startswith(("postgres://", "postgresql://"))


INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError) if PG_AVAILABLE else (sqlite3.IntegrityError,)

app = Flask(__name__, template_folder=str(BASE / "templates"))
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "loop-social-media-secure-key-2026"),
    MAX_CONTENT_LENGTH=100 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)


def _format_pg_row(row):
    """PostgreSQL satırlarındaki datetime değerlerini ISO/string formatına dönüştürür."""
    if not isinstance(row, dict):
        return row
    res = {}
    for k, v in row.items():
        if hasattr(v, "strftime"):
            res[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        else:
            res[k] = v
    return res


class PgCursorWrapper:
    def __init__(self, cursor, conn):
        self._cur = cursor
        self._conn = conn
        self.lastrowid = None

    def execute(self, query, params=None):
        q = query.strip()
        # SQLite'a özgü BEGIN IMMEDIATE komutu PostgreSQL'de no-op
        if q.upper().startswith("BEGIN IMMEDIATE"):
            return self

        # SQLite parametre işaretçisi '?' -> PostgreSQL '%s'
        q = q.replace("?", "%s")

        # INSERT OR IGNORE INTO -> PostgreSQL ON CONFLICT DO NOTHING dönüşümü
        if "INSERT OR IGNORE INTO" in q.upper():
            q = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", q, flags=re.IGNORECASE)
            if "ON CONFLICT" not in q.upper():
                q += " ON CONFLICT DO NOTHING"

        # Otomatik lastrowid desteği: RETURNING id ekle
        is_insert = q.upper().startswith("INSERT INTO")
        has_returning = "RETURNING" in q.upper()
        if is_insert and not has_returning and any(tbl in q.lower() for tbl in ("users", "posts", "comments")):
            q += " RETURNING id"
            if params is not None:
                self._cur.execute(q, params)
            else:
                self._cur.execute(q)
            try:
                row = self._cur.fetchone()
                if row:
                    self.lastrowid = row.get("id") if isinstance(row, dict) else row[0]
            except Exception:
                pass
            return self

        if params is not None:
            self._cur.execute(q, params)
        else:
            self._cur.execute(q)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return _format_pg_row(row) if row else None

    def fetchall(self):
        rows = self._cur.fetchall()
        return [_format_pg_row(r) for r in rows] if rows else []

    def __iter__(self):
        for r in self._cur:
            yield _format_pg_row(r)


class PgConnectionWrapper:
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, query, params=None):
        cur = self.cursor()
        return cur.execute(query, params)

    def cursor(self):
        return PgCursorWrapper(
            self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor),
            self._conn,
        )

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()


def db():
    if "db" not in g:
        if is_postgres():
            if not PG_AVAILABLE:
                raise RuntimeError("PostgreSQL bağlantısı için psycopg2 gereklidir: pip install psycopg2-binary")
            dsn = (DATABASE_URL or "").strip()
            if dsn.startswith("postgres://"):
                dsn = "postgresql://" + dsn[len("postgres://"):]
            if "sslmode" not in dsn:
                separator = "&" if "?" in dsn else "?"
                dsn += f"{separator}sslmode=require"
            raw_conn = psycopg2.connect(dsn)
            g.db = PgConnectionWrapper(raw_conn)
        else:
            g.db = sqlite3.connect(DATA / "social.db", timeout=20)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    if is_postgres():
        conn = db()
        with conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                avatar TEXT,
                password TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS posts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                caption TEXT NOT NULL,
                media TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                premium INTEGER NOT NULL DEFAULT 0,
                price INTEGER NOT NULL DEFAULT 49,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS likes (
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, post_id)
            );

            CREATE TABLE IF NOT EXISTS saves (
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, post_id)
            );

            CREATE TABLE IF NOT EXISTS follows (
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                target_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, target_id),
                CHECK (user_id != target_id)
            );

            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS unlocks (
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL,
                payment_mode TEXT NOT NULL DEFAULT 'simulation',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, post_id)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
            CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
            CREATE INDEX IF NOT EXISTS idx_follows_target ON follows(target_id);
            CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
            """)

            cur = conn.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'comments' AND column_name = 'parent_id'
            """)
            if not cur.fetchone():
                conn.execute("ALTER TABLE comments ADD COLUMN parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE")
    else:
        db().executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            bio TEXT NOT NULL DEFAULT '',
            avatar TEXT,
            password TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            caption TEXT NOT NULL,
            media TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            premium INTEGER NOT NULL DEFAULT 0,
            price INTEGER NOT NULL DEFAULT 49,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS likes (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, post_id)
        );

        CREATE TABLE IF NOT EXISTS saves (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, post_id)
        );

        CREATE TABLE IF NOT EXISTS follows (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            target_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, target_id),
            CHECK (user_id != target_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS unlocks (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL,
            payment_mode TEXT NOT NULL DEFAULT 'simulation',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, post_id)
        );

        CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id);
        CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
        CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
        CREATE INDEX IF NOT EXISTS idx_follows_target ON follows(target_id);
        """)

        comment_cols = [c["name"] for c in db().execute("PRAGMA table_info(comments)").fetchall()]
        if "parent_id" not in comment_cols:
            db().execute("ALTER TABLE comments ADD COLUMN parent_id INTEGER DEFAULT NULL")
            db().commit()
        db().execute("CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id)")
        db().commit()

    admin_row = db().execute(
        "SELECT id FROM users WHERE username = 'admin'"
    ).fetchone()
    if not admin_row:
        db().execute(
            """INSERT INTO users(username, name, bio, password, is_admin)
               VALUES (?, ?, ?, ?, 1)""",
            (
                "admin",
                "Studio Admin",
                "İçerik stüdyosuna hoş geldin.",
                generate_password_hash("123"),
            ),
        )
        db().commit()
    else:
        db().execute(
            "UPDATE users SET password = ?, is_admin = 1 WHERE username = 'admin'",
            (generate_password_hash("123"),),
        )
        db().commit()

    db_type_desc = f"PostgreSQL ({DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'Remote'})" if is_postgres() else "SQLite (data/social.db)"
    print("\n" + "=" * 55)
    print(f"AKTİF VERİTABANI: {db_type_desc}")
    print("VARSAYILAN YÖNETİCİ HESABI")
    print("Kullanıcı adı: admin")
    print("Şifre: 123")
    print("=" * 55 + "\n")


def current_user():
    if "current_user" not in g:
        g.current_user = db().execute(
            "SELECT * FROM users WHERE id = ?",
            (session.get("uid", 0),),
        ).fetchone()
    return g.current_user


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            abort(401, description="Önce giriş yapmalısın.")
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    @require_login
    def wrapper(*args, **kwargs):
        if not current_user()["is_admin"]:
            abort(403, description="Bu işlem yönetici yetkisi gerektirir.")
        return fn(*args, **kwargs)
    return wrapper


@app.before_request
def csrf_protection():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(supplied, session["csrf"]):
            abort(403, description="Oturum doğrulaması başarısız. Sayfayı yenile.")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    return response


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(error):
    message = (
        "Yükleme en fazla 100 MB olabilir."
        if error.code == 413 else error.description
    )
    return jsonify(error=message), error.code


@app.errorhandler(500)
def internal_error(_error):
    return jsonify(error="Sunucu hatası oluştu. Terminal kayıtlarını kontrol et."), 500


def payload():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        abort(400, description="Geçerli bir JSON nesnesi gerekli.")
    return value


def text(value, maximum, minimum=0):
    value = str(value or "").strip()
    if len(value) < minimum or len(value) > maximum:
        abort(
            400,
            description=f"Metin uzunluğu {minimum}-{maximum} karakter olmalı.",
        )
    return value


def integer(value, minimum=1, maximum=1_000_000_000):
    try:
        number = int(value)
    except (ValueError, TypeError):
        abort(400, description="Geçerli bir sayı gir.")
    if not minimum <= number <= maximum:
        abort(400, description="Sayı izin verilen aralığın dışında.")
    return number


def get_post(post_id):
    row = db().execute(
        "SELECT * FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    if not row:
        abort(404, description="İçerik bulunamadı.")
    return row


def can_view(post, user=None):
    user = user if user is not None else current_user()
    if not post["premium"]:
        return True
    if not user:
        return False
    if user["is_admin"] or user["id"] == post["user_id"]:
        return True
    return db().execute(
        "SELECT 1 FROM unlocks WHERE user_id = ? AND post_id = ?",
        (user["id"], post["id"]),
    ).fetchone() is not None


def public_user(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "bio": row["bio"],
        "avatar": f"/media/{row['avatar']}" if row["avatar"] else None,
        "is_admin": bool(row["is_admin"]),
    }


def store_upload(file, images_only=False):
    if not file or not file.filename:
        abort(400, description="Bir medya dosyası seç.")

    ext = Path(file.filename).suffix.lower()
    head = file.stream.read(32)
    file.stream.seek(0)

    # Basic format checks. Uploaded files are never executed.
    formats = {
        ".jpg": ("image", head.startswith(b"\xff\xd8\xff")),
        ".jpeg": ("image", head.startswith(b"\xff\xd8\xff")),
        ".png": ("image", head.startswith(b"\x89PNG\r\n\x1a\n")),
        ".webp": (
            "image",
            head[:4] == b"RIFF" and head[8:12] == b"WEBP",
        ),
        ".mp4": ("video", head[4:8] == b"ftyp"),
        ".webm": ("video", head.startswith(b"\x1a\x45\xdf\xa3")),
    }
    info = formats.get(ext)
    if not info or not info[1] or (images_only and info[0] != "image"):
        abort(
            400,
            description="Desteklenen biçimler: JPG, PNG, WebP, MP4 ve WebM.",
        )

    filename = secrets.token_hex(20) + ext
    destination = UPLOADS / filename
    try:
        file.save(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return filename, info[0]


def create_user(data, avatar=None):
    username = text(data.get("username"), 24, 3).lower()
    if not re.fullmatch(r"[a-z0-9_]+", username):
        abort(
            400,
            description="Kullanıcı adı yalnızca a-z, 0-9 ve _ içerebilir.",
        )

    password = str(data.get("password") or "")
    if not password:
        abort(400, description="Şifre boş olamaz.")

    name = text(data.get("name"), 60, 1)
    bio = text(data.get("bio"), 300)

    try:
        cursor = db().execute(
            """INSERT INTO users(username, name, bio, avatar, password)
               VALUES (?, ?, ?, ?, ?)""",
            (username, name, bio, avatar, generate_password_hash(password)),
        )
        db().commit()
        return cursor.lastrowid
    except INTEGRITY_ERRORS:
        db().rollback()
        abort(400, description="Bu kullanıcı adı zaten kullanılıyor.")


@app.get("/")
def index():
    return render_template("index.html", csrf_token=session["csrf"])


@app.get("/api/state")
def state():
    user = current_user()
    uid = user["id"] if user else 0

    profiles = []
    for row in db().execute(
        """SELECT u.*,
           (SELECT COUNT(*) FROM posts WHERE user_id=u.id) AS post_count,
           (SELECT COUNT(*) FROM follows WHERE target_id=u.id) AS followers,
           EXISTS(SELECT 1 FROM follows
                  WHERE user_id=? AND target_id=u.id) AS following
           FROM users u ORDER BY u.id DESC""",
        (uid,),
    ):
        item = public_user(row)
        item.update(
            post_count=row["post_count"],
            followers=row["followers"],
            following=bool(row["following"]),
        )
        profiles.append(item)

    posts = []
    rows = db().execute(
        """SELECT p.*,
           (SELECT COUNT(*) FROM likes WHERE post_id=p.id) AS like_count,
           (SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count,
           EXISTS(SELECT 1 FROM likes
                  WHERE user_id=? AND post_id=p.id) AS liked,
           EXISTS(SELECT 1 FROM saves
                  WHERE user_id=? AND post_id=p.id) AS saved
           FROM posts p ORDER BY p.id DESC""",
        (uid, uid),
    ).fetchall()

    for row in rows:
        visible = can_view(row, user)
        posts.append({
            "id": row["id"],
            "user_id": row["user_id"],
            "caption": row["caption"],
            "kind": row["kind"],
            "premium": bool(row["premium"]),
            "price": row["price"],
            "locked": not visible,
            "media": f"/media/{row['media']}" if visible else None,
            "likes": row["like_count"],
            "comments": row["comment_count"],
            "liked": bool(row["liked"]),
            "saved": bool(row["saved"]),
            "created_at": row["created_at"],
        })

    return jsonify(
        me=public_user(user) if user else None,
        profiles=profiles,
        posts=posts,
        csrf=session["csrf"],
    )


@app.post("/api/register")
def register():
    uid = create_user(payload())
    session.clear()
    session["uid"] = uid
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify(ok=True, csrf=session["csrf"])


@app.post("/api/login")
def login():
    data = payload()
    username = text(data.get("username"), 24).lower()
    password = str(data.get("password") or "")
    user = db().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()

    if not user or not check_password_hash(user["password"], password):
        abort(401, description="Kullanıcı adı veya şifre hatalı.")

    session.clear()
    session["uid"] = user["id"]
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify(ok=True, csrf=session["csrf"])


@app.post("/api/logout")
def logout():
    session.clear()
    session["csrf"] = secrets.token_urlsafe(32)
    return jsonify(ok=True, csrf=session["csrf"])


@app.post("/api/unlock/<int:post_id>")
@require_login
def unlock(post_id):
    post = get_post(post_id)
    if post["premium"]:
        db().execute(
            """INSERT OR IGNORE INTO unlocks(user_id, post_id, amount)
               VALUES (?, ?, ?)""",
            (current_user()["id"], post_id, post["price"]),
        )
        db().commit()

    return jsonify(ok=True, message="İçeriğin kilidi başarıyla açıldı.")


@app.post("/api/post/<int:post_id>/<action>")
@require_login
def toggle_post_action(post_id, action):
    if action not in {"like", "save"}:
        abort(404, description="İşlem bulunamadı.")

    post = get_post(post_id)
    if action == "like" and not can_view(post):
        abort(403, description="Önce içeriğin kilidini aç.")

    table = {"like": "likes", "save": "saves"}[action]
    values = (current_user()["id"], post_id)
    connection = db()

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            f"SELECT 1 FROM {table} WHERE user_id=? AND post_id=?",
            values,
        ).fetchone()
        if exists:
            connection.execute(
                f"DELETE FROM {table} WHERE user_id=? AND post_id=?",
                values,
            )
        else:
            connection.execute(
                f"INSERT INTO {table}(user_id, post_id) VALUES (?, ?)",
                values,
            )
    return jsonify(ok=True)


@app.post("/api/follow/<int:target_id>")
@require_login
def follow(target_id):
    uid = current_user()["id"]
    if uid == target_id:
        abort(400, description="Kendini takip edemezsin.")
    if not db().execute(
        "SELECT 1 FROM users WHERE id=?", (target_id,)
    ).fetchone():
        abort(404, description="Profil bulunamadı.")

    connection = db()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            "SELECT 1 FROM follows WHERE user_id=? AND target_id=?",
            (uid, target_id),
        ).fetchone()
        if exists:
            connection.execute(
                "DELETE FROM follows WHERE user_id=? AND target_id=?",
                (uid, target_id),
            )
        else:
            connection.execute(
                "INSERT INTO follows(user_id, target_id) VALUES (?, ?)",
                (uid, target_id),
            )
    return jsonify(ok=True)


@app.route("/api/comments/<int:post_id>", methods=["GET", "POST"])
def comments(post_id):
    post = get_post(post_id)
    if not can_view(post):
        abort(403, description="Yorumlar için önce içeriğin kilidini aç.")

    if request.method == "POST":
        if not current_user():
            abort(401, description="Yorum yazmak için giriş yap.")
        body = text(payload().get("body"), 500, 1)
        author_id = current_user()["id"]

        if current_user()["is_admin"]:
            as_user_id = payload().get("as_user_id")
            if as_user_id:
                try:
                    target_uid = int(as_user_id)
                    exists = db().execute("SELECT id FROM users WHERE id=?", (target_uid,)).fetchone()
                    if exists:
                        author_id = exists["id"]
                except (ValueError, TypeError):
                    pass

        parent_id = payload().get("parent_id")
        if parent_id is not None:
            try:
                parent_id = int(parent_id)
                p_comm = db().execute(
                    "SELECT id FROM comments WHERE id=? AND post_id=?",
                    (parent_id, post_id),
                ).fetchone()
                if not p_comm:
                    parent_id = None
            except (ValueError, TypeError):
                parent_id = None
        else:
            parent_id = None

        db().execute(
            "INSERT INTO comments(user_id, post_id, body, parent_id) VALUES (?, ?, ?, ?)",
            (author_id, post_id, body, parent_id),
        )
        db().commit()

    rows = db().execute(
        """SELECT c.id, c.body, c.created_at, c.parent_id,
                  u.id AS user_id, u.name, u.username, u.avatar,
                  pu.username AS reply_to_username
           FROM comments c
           JOIN users u ON u.id = c.user_id
           LEFT JOIN comments pc ON pc.id = c.parent_id
           LEFT JOIN users pu ON pu.id = pc.user_id
           WHERE c.post_id = ?
           ORDER BY c.id ASC LIMIT 250""",
        (post_id,),
    ).fetchall()
    return jsonify(comments=[dict(row) for row in rows])


@app.delete("/api/comments/<int:comment_id>")
@require_login
def delete_comment(comment_id):
    comment = db().execute(
        "SELECT * FROM comments WHERE id=?", (comment_id,)
    ).fetchone()
    if not comment:
        abort(404, description="Yorum bulunamadı.")

    if not current_user()["is_admin"] and comment["user_id"] != current_user()["id"]:
        abort(403, description="Bu yorumu silme yetkin yok.")

    post_id = comment["post_id"]
    db().execute("DELETE FROM comments WHERE id=? OR parent_id=?", (comment_id, comment_id))
    db().commit()
    return jsonify(ok=True, post_id=post_id)


@app.post("/api/admin/profiles")
@require_admin
def admin_profile():
    avatar = None
    try:
        file = request.files.get("avatar")
        if file and file.filename:
            avatar, _kind = store_upload(file, images_only=True)
        uid = create_user(request.form, avatar)
    except Exception:
        if avatar:
            (UPLOADS / avatar).unlink(missing_ok=True)
        raise
    return jsonify(ok=True, id=uid)


@app.post("/api/admin/posts")
@require_admin
def admin_post():
    user_id = integer(request.form.get("user_id"))
    if not db().execute(
        "SELECT 1 FROM users WHERE id=?", (user_id,)
    ).fetchone():
        abort(400, description="Geçerli bir profil seç.")

    caption = text(request.form.get("caption"), 2000)
    premium = request.form.get("premium") == "1"
    price = integer(request.form.get("price", 49), 1, 100000)
    media, kind = store_upload(request.files.get("media"))

    try:
        db().execute(
            """INSERT INTO posts(user_id, caption, media, kind, premium, price)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, caption, media, kind, int(premium), price),
        )
        db().commit()
    except Exception:
        db().rollback()
        (UPLOADS / media).unlink(missing_ok=True)
        raise

    return jsonify(ok=True)


@app.delete("/api/admin/posts/<int:post_id>")
@require_admin
def delete_post(post_id):
    post = get_post(post_id)
    db().execute("DELETE FROM posts WHERE id=?", (post_id,))
    db().commit()
    (UPLOADS / post["media"]).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.post("/api/admin/reset-db")
@require_admin
def reset_db():
    # 1. Sunucudaki tüm medya dosyalarını sil
    if UPLOADS.exists():
        for f in UPLOADS.iterdir():
            if f.is_file():
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass

    # 2. Veritabanındaki tüm tabloları kaldır
    tables = ["unlocks", "comments", "follows", "saves", "likes", "posts", "users"]
    connection = db()
    if is_postgres():
        with connection:
            for tbl in tables:
                connection.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    else:
        connection.execute("PRAGMA foreign_keys = OFF")
        for tbl in tables:
            connection.execute(f"DROP TABLE IF EXISTS {tbl}")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")

    # 3. Şemayı ve varsayılan yönetici hesabını ilk günkü gibi yeniden kur
    init_db()

    # 4. Admin oturumunu yenilenen admin kullanıcısına bağla
    admin_user = db().execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if admin_user:
        session["uid"] = admin_user["id"]
        g.current_user = admin_user

    return jsonify(ok=True, message="Tüm veritabanı ve yüklenen medyalar sıfırlandı. Proje ilk kurulum haline getirildi.")



@app.get("/media/<filename>")
def media(filename):
    if Path(filename).name != filename:
        abort(404, description="Dosya bulunamadı.")

    avatar = db().execute(
        "SELECT 1 FROM users WHERE avatar=?", (filename,)
    ).fetchone()

    if not avatar:
        post = db().execute(
            "SELECT * FROM posts WHERE media=?", (filename,)
        ).fetchone()
        if not post:
            abort(404, description="Dosya bulunamadı.")
        if not can_view(post):
            abort(403, description="Bu içeriğe erişim iznin yok.")

    return send_from_directory(
        str(UPLOADS), filename, conditional=True, max_age=0
    )


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
