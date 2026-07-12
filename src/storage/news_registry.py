"""News registry (NEWS-1 #341, epic #340) — типизированный слой над
news_posts / news_post_revisions / news_reads (migrations/024_news.sql).

Зеркалит паттерн upload_registry: Postgres-бэкенд для prod + локальный
JSON-файловый для dev/тестов; выбор через DATABASE_URL. Live-гейтинг —
ПРЕДИКАТ (status=published AND publish_at<=now AND expire_at не прошёл),
никакого крона (меньше точек тихого отказа).

Аудит и рендер body_html здесь НЕ живут: этим владеет API-слой (NEWS-2)
через record_event и src/cms.render_markdown — реестр только хранит.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CATEGORIES = {"release", "improvement", "maintenance", "tip", "announcement"}
STATUSES = {"draft", "published", "archived"}
IMPORTANCE = {"normal", "important"}

DRAFT, PUBLISHED, ARCHIVED = "draft", "published", "archived"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_post_id() -> str:
    return uuid.uuid4().hex


@dataclass
class NewsPost:
    id: str
    slug: str
    title: str
    body_md: str
    body_html: str
    category: str
    author_admin_id: str
    summary: str = ""
    status: str = DRAFT
    pinned: bool = False
    importance: str = "normal"
    publish_at: Optional[datetime] = None
    expire_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def is_live(self, at: Optional[datetime] = None) -> bool:
        at = at or _now()
        if self.status != PUBLISHED:
            return False
        if self.publish_at is not None and self.publish_at > at:
            return False
        if self.expire_at is not None and self.expire_at <= at:
            return False
        return True


@dataclass
class NewsRevision:
    id: int
    post_id: str
    title: str
    body_md: str
    editor_admin_id: str
    created_at: datetime


# колонки, которые API-слою разрешено менять через update_fields
_MUTABLE = {
    "slug", "title", "summary", "body_md", "body_html", "category",
    "status", "pinned", "importance", "publish_at", "expire_at",
    "published_at",
}


class NewsRegistry:
    """Интерфейс; реализации ниже."""

    def create(self, post: NewsPost) -> None: ...
    def get(self, post_id: str) -> Optional[NewsPost]: ...
    def get_by_slug(self, slug: str) -> Optional[NewsPost]: ...
    def update_fields(self, post_id: str, **fields) -> NewsPost: ...
    def list_admin(self, status: Optional[str] = None,
                   category: Optional[str] = None,
                   pinned: Optional[bool] = None,
                   limit: int = 100) -> list[NewsPost]: ...
    def list_live(self, limit: int = 20, offset: int = 0) -> list[NewsPost]: ...
    def add_revision(self, post_id: str, title: str, body_md: str,
                     editor_admin_id: str) -> None: ...
    def list_revisions(self, post_id: str, limit: int = 50) -> list[NewsRevision]: ...
    def mark_read(self, client_id: str, post_id: str) -> None: ...
    def read_post_ids(self, client_id: str) -> set[str]: ...

    def unread_count(self, client_id: str) -> int:
        read = self.read_post_ids(client_id)
        return sum(1 for p in self.list_live(limit=500) if p.id not in read)

    @staticmethod
    def _check_fields(fields: dict) -> None:
        bad = set(fields) - _MUTABLE
        if bad:
            raise ValueError(f"Cannot update columns: {bad}")
        if "category" in fields and fields["category"] not in CATEGORIES:
            raise ValueError(f"Unknown category: {fields['category']!r}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"Unknown status: {fields['status']!r}")
        if "importance" in fields and fields["importance"] not in IMPORTANCE:
            raise ValueError(f"Unknown importance: {fields['importance']!r}")


# ── Postgres ──────────────────────────────────────────────────────────────

class PostgresNewsRegistry(NewsRegistry):

    def __init__(self, database_url: str):
        import psycopg2
        import psycopg2.extras
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self._url = database_url
        # Schema: migrations/024_news.sql

    def _conn(self):
        return self._psycopg2.connect(self._url)

    @staticmethod
    def _row_to_post(row: dict) -> NewsPost:
        return NewsPost(**{k: row[k] for k in NewsPost.__dataclass_fields__})

    def create(self, post: NewsPost) -> None:
        cols = list(NewsPost.__dataclass_fields__)
        sql = (f"INSERT INTO news_posts ({', '.join(cols)}) "
               f"VALUES ({', '.join(['%s'] * len(cols))})")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, [getattr(post, c) for c in cols])
            conn.commit()

    def get(self, post_id: str) -> Optional[NewsPost]:
        return self._one("id = %s", (post_id,))

    def get_by_slug(self, slug: str) -> Optional[NewsPost]:
        return self._one("slug = %s", (slug,))

    def _one(self, where: str, params: tuple) -> Optional[NewsPost]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM news_posts WHERE {where}", params)
            row = cur.fetchone()
        return None if row is None else self._row_to_post(dict(row))

    def update_fields(self, post_id: str, **fields) -> NewsPost:
        self._check_fields(fields)
        set_parts = [f"{k} = %s" for k in fields] + ["updated_at = NOW()"]
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE news_posts SET {', '.join(set_parts)} "
                f"WHERE id = %s RETURNING *",
                [*fields.values(), post_id],
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise KeyError(f"news post {post_id!r} not found")
        return self._row_to_post(dict(row))

    def list_admin(self, status=None, category=None, pinned=None,
                   limit: int = 100) -> list[NewsPost]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if category is not None:
            clauses.append("category = %s")
            params.append(category)
        if pinned is not None:
            clauses.append("pinned = %s")
            params.append(pinned)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM news_posts {where} "
                f"ORDER BY created_at DESC LIMIT %s", [*params, limit])
            rows = cur.fetchall()
        return [self._row_to_post(dict(r)) for r in rows]

    def list_live(self, limit: int = 20, offset: int = 0) -> list[NewsPost]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM news_posts
                WHERE status = 'published'
                  AND (publish_at IS NULL OR publish_at <= now())
                  AND (expire_at IS NULL OR expire_at > now())
                ORDER BY pinned DESC, publish_at DESC NULLS LAST,
                         published_at DESC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
        return [self._row_to_post(dict(r)) for r in rows]

    def add_revision(self, post_id: str, title: str, body_md: str,
                     editor_admin_id: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_post_revisions "
                "(post_id, title, body_md, editor_admin_id) "
                "VALUES (%s, %s, %s, %s)",
                (post_id, title, body_md, editor_admin_id),
            )
            conn.commit()

    def list_revisions(self, post_id: str, limit: int = 50) -> list[NewsRevision]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM news_post_revisions WHERE post_id = %s "
                "ORDER BY created_at DESC LIMIT %s", (post_id, limit))
            rows = cur.fetchall()
        return [NewsRevision(**dict(r)) for r in rows]

    def mark_read(self, client_id: str, post_id: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_reads (client_id, post_id) VALUES (%s, %s) "
                "ON CONFLICT (client_id, post_id) DO NOTHING",
                (client_id, post_id),
            )
            conn.commit()

    def read_post_ids(self, client_id: str) -> set[str]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT post_id FROM news_reads WHERE client_id = %s",
                        (client_id,))
            return {r[0] for r in cur.fetchall()}


# ── Local JSON (dev/tests) ────────────────────────────────────────────────

class LocalFileNewsRegistry(NewsRegistry):
    """Файловый бэкенд для dev/тестов — та же семантика, без Postgres."""

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path or os.environ.get(
            "NEWS_REGISTRY_PATH",
            str(Path(os.environ.get("ARTIFACTS_DIR", "artifacts")) / "news.json"),
        ))
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self._path.exists():
            return {"posts": {}, "revisions": [], "reads": {}}
        return json.loads(self._path.read_text() or
                          '{"posts": {}, "revisions": [], "reads": {}}')

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=_json_dt))
        tmp.replace(self._path)

    def create(self, post: NewsPost) -> None:
        with self._lock:
            data = self._load()
            if any(p["slug"] == post.slug for p in data["posts"].values()):
                raise ValueError(f"slug {post.slug!r} already exists")
            data["posts"][post.id] = asdict(post)
            self._save(data)

    def get(self, post_id: str) -> Optional[NewsPost]:
        row = self._load()["posts"].get(post_id)
        return _post_from_json(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[NewsPost]:
        for row in self._load()["posts"].values():
            if row["slug"] == slug:
                return _post_from_json(row)
        return None

    def update_fields(self, post_id: str, **fields) -> NewsPost:
        self._check_fields(fields)
        with self._lock:
            data = self._load()
            row = data["posts"].get(post_id)
            if row is None:
                raise KeyError(f"news post {post_id!r} not found")
            row.update({k: _json_dt(v) if isinstance(v, datetime) else v
                        for k, v in fields.items()})
            row["updated_at"] = _json_dt(_now())
            self._save(data)
            return _post_from_json(row)

    def list_admin(self, status=None, category=None, pinned=None,
                   limit: int = 100) -> list[NewsPost]:
        posts = [_post_from_json(r) for r in self._load()["posts"].values()]
        if status is not None:
            posts = [p for p in posts if p.status == status]
        if category is not None:
            posts = [p for p in posts if p.category == category]
        if pinned is not None:
            posts = [p for p in posts if p.pinned == pinned]
        posts.sort(key=lambda p: p.created_at, reverse=True)
        return posts[:limit]

    def list_live(self, limit: int = 20, offset: int = 0) -> list[NewsPost]:
        now = _now()
        posts = [p for p in
                 (_post_from_json(r) for r in self._load()["posts"].values())
                 if p.is_live(now)]
        far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
        posts.sort(key=lambda p: (
            not p.pinned,
            -(p.publish_at or p.published_at or far_past).timestamp(),
        ))
        return posts[offset:offset + limit]

    def add_revision(self, post_id: str, title: str, body_md: str,
                     editor_admin_id: str) -> None:
        with self._lock:
            data = self._load()
            data["revisions"].append({
                "id": len(data["revisions"]) + 1,
                "post_id": post_id, "title": title, "body_md": body_md,
                "editor_admin_id": editor_admin_id,
                "created_at": _json_dt(_now()),
            })
            self._save(data)

    def list_revisions(self, post_id: str, limit: int = 50) -> list[NewsRevision]:
        revs = [r for r in self._load()["revisions"] if r["post_id"] == post_id]
        revs.sort(key=lambda r: r["created_at"], reverse=True)
        return [NewsRevision(**{**r, "created_at": _parse_dt(r["created_at"])})
                for r in revs[:limit]]

    def mark_read(self, client_id: str, post_id: str) -> None:
        with self._lock:
            data = self._load()
            data["reads"].setdefault(client_id, {})
            data["reads"][client_id].setdefault(post_id, _json_dt(_now()))
            self._save(data)

    def read_post_ids(self, client_id: str) -> set[str]:
        return set(self._load()["reads"].get(client_id, {}))


def _json_dt(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _parse_dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


def _post_from_json(row: dict) -> NewsPost:
    kw = dict(row)
    for k in ("publish_at", "expire_at", "published_at",
              "created_at", "updated_at"):
        kw[k] = _parse_dt(kw.get(k))
    return NewsPost(**kw)


# ── фабрика ──────────────────────────────────────────────────────────────

_registry: Optional[NewsRegistry] = None
_registry_lock = threading.Lock()


def get_news_registry() -> NewsRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            url = os.environ.get("DATABASE_URL")
            _registry = (PostgresNewsRegistry(url) if url
                         else LocalFileNewsRegistry())
        return _registry


def reset_registry_for_tests() -> None:
    global _registry
    with _registry_lock:
        _registry = None
