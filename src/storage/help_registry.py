"""Help Center registry (HC-1 #333, epic #332) — типизированный слой над
help_categories / help_articles / revisions / feedback / search_log
(migrations/025_help_center.sql).

Паттерн news_registry: Postgres для prod + локальный JSON для dev/тестов
(выбор по DATABASE_URL). Рендер body_html и аудит живут на API-слое
(HC-2/3) через src/cms и record_event — реестр только хранит.

PII-free: feedback несёт только voter_hash (один голос на сессию —
UNIQUE(article_id, voter_hash)), search-лог — строку запроса и счётчик.
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

STATUSES = {"draft", "published", "archived"}
DRAFT, PUBLISHED, ARCHIVED = "draft", "published", "archived"
DEFAULT_LOCALE = "ru"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_help_id() -> str:
    return uuid.uuid4().hex


@dataclass
class HelpCategory:
    id: str
    slug: str
    title: str
    locale: str = DEFAULT_LOCALE
    description: str = ""
    icon: str = ""
    sort_order: int = 0
    parent_id: Optional[str] = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class HelpArticle:
    id: str
    slug: str
    category_id: str
    title: str
    body_md: str
    body_html: str
    author_admin_id: str
    locale: str = DEFAULT_LOCALE
    excerpt: str = ""
    status: str = DRAFT
    sort_order: int = 0
    seo_title: str = ""
    seo_description: str = ""
    view_count: int = 0
    published_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class HelpRevision:
    id: int
    article_id: str
    title: str
    body_md: str
    editor_admin_id: str
    created_at: datetime


_CATEGORY_MUTABLE = {"slug", "title", "description", "icon", "sort_order",
                     "parent_id"}
_ARTICLE_MUTABLE = {"slug", "category_id", "title", "body_md", "body_html",
                    "excerpt", "status", "sort_order", "seo_title",
                    "seo_description", "published_at"}


class HelpRegistry:
    """Интерфейс; реализации ниже."""

    # категории
    def create_category(self, cat: HelpCategory) -> None: ...
    def get_category(self, cat_id: str) -> Optional[HelpCategory]: ...
    def get_category_by_slug(self, slug: str,
                             locale: str = DEFAULT_LOCALE) -> Optional[HelpCategory]: ...
    def update_category(self, cat_id: str, **fields) -> HelpCategory: ...
    def list_categories(self, locale: str = DEFAULT_LOCALE) -> list[HelpCategory]: ...

    # статьи
    def create_article(self, art: HelpArticle) -> None: ...
    def get_article(self, art_id: str) -> Optional[HelpArticle]: ...
    def get_article_by_slug(self, slug: str,
                            locale: str = DEFAULT_LOCALE) -> Optional[HelpArticle]: ...
    def update_article(self, art_id: str, **fields) -> HelpArticle: ...
    def list_articles_admin(self, category_id: Optional[str] = None,
                            status: Optional[str] = None,
                            limit: int = 200) -> list[HelpArticle]: ...
    def list_published(self, category_id: Optional[str] = None,
                       locale: str = DEFAULT_LOCALE) -> list[HelpArticle]: ...
    def increment_views(self, art_id: str) -> None: ...

    # ревизии
    def add_revision(self, article_id: str, title: str, body_md: str,
                     editor_admin_id: str) -> None: ...
    def list_revisions(self, article_id: str,
                       limit: int = 50) -> list[HelpRevision]: ...

    # фидбек (True = голос записан, False = уже голосовал).
    # feedback_summary/list_feedback_comments (HC-6) НИКОГДА не отдают
    # voter_hash — он не покидает слой хранения.
    def add_feedback(self, article_id: str, helpful: bool,
                     comment: Optional[str], voter_hash: str) -> bool: ...
    def feedback_stats(self, article_id: str) -> dict: ...
    def feedback_summary(self) -> dict: ...
    def list_feedback_comments(self, limit: int = 100) -> list[dict]: ...

    # поиск (HC-5). search_published отдаёт пары (статья, сниппет);
    # совпадения в сниппете помечены сентинелами [[…]] — сервер НЕ
    # собирает HTML (фронт сам рендерит <mark>), поэтому XSS невозможен.
    def search_published(self, query: str,
                         locale: str = DEFAULT_LOCALE,
                         limit: int = 20) -> list[tuple[HelpArticle, str]]: ...
    def log_search(self, query: str, results_count: int) -> None: ...
    def zero_result_queries(self, limit: int = 50) -> list[dict]: ...
    def top_queries(self, limit: int = 50) -> list[dict]: ...

    @staticmethod
    def _check_fields(fields: dict, allowed: set) -> None:
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Cannot update columns: {bad}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"Unknown status: {fields['status']!r}")


# ── Postgres ──────────────────────────────────────────────────────────────

class PostgresHelpRegistry(HelpRegistry):

    def __init__(self, database_url: str):
        import psycopg2
        import psycopg2.extras
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self._url = database_url
        # Schema: migrations/025_help_center.sql

    def _conn(self):
        return self._psycopg2.connect(self._url)

    @staticmethod
    def _row_cat(row: dict) -> HelpCategory:
        return HelpCategory(**{k: row[k] for k in HelpCategory.__dataclass_fields__})

    @staticmethod
    def _row_art(row: dict) -> HelpArticle:
        return HelpArticle(**{k: row[k] for k in HelpArticle.__dataclass_fields__})

    # категории
    def create_category(self, cat: HelpCategory) -> None:
        cols = list(HelpCategory.__dataclass_fields__)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO help_categories ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})",
                [getattr(cat, c) for c in cols])
            conn.commit()

    def get_category(self, cat_id: str) -> Optional[HelpCategory]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM help_categories WHERE id = %s", (cat_id,))
            row = cur.fetchone()
        return None if row is None else self._row_cat(dict(row))

    def get_category_by_slug(self, slug, locale=DEFAULT_LOCALE):
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM help_categories WHERE slug = %s AND locale = %s",
                        (slug, locale))
            row = cur.fetchone()
        return None if row is None else self._row_cat(dict(row))

    def update_category(self, cat_id: str, **fields) -> HelpCategory:
        self._check_fields(fields, _CATEGORY_MUTABLE)
        set_parts = [f"{k} = %s" for k in fields] + ["updated_at = NOW()"]
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE help_categories SET {', '.join(set_parts)} "
                f"WHERE id = %s RETURNING *", [*fields.values(), cat_id])
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise KeyError(f"help category {cat_id!r} not found")
        return self._row_cat(dict(row))

    def list_categories(self, locale=DEFAULT_LOCALE) -> list[HelpCategory]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM help_categories WHERE locale = %s "
                        "ORDER BY sort_order, title", (locale,))
            rows = cur.fetchall()
        return [self._row_cat(dict(r)) for r in rows]

    # статьи
    def create_article(self, art: HelpArticle) -> None:
        cols = list(HelpArticle.__dataclass_fields__)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO help_articles ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})",
                [getattr(art, c) for c in cols])
            conn.commit()

    def get_article(self, art_id: str) -> Optional[HelpArticle]:
        return self._one_art("id = %s", (art_id,))

    def get_article_by_slug(self, slug, locale=DEFAULT_LOCALE):
        return self._one_art("slug = %s AND locale = %s", (slug, locale))

    def _one_art(self, where: str, params: tuple) -> Optional[HelpArticle]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM help_articles WHERE {where}", params)
            row = cur.fetchone()
        return None if row is None else self._row_art(dict(row))

    def update_article(self, art_id: str, **fields) -> HelpArticle:
        self._check_fields(fields, _ARTICLE_MUTABLE)
        set_parts = [f"{k} = %s" for k in fields] + ["updated_at = NOW()"]
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE help_articles SET {', '.join(set_parts)} "
                f"WHERE id = %s RETURNING *", [*fields.values(), art_id])
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise KeyError(f"help article {art_id!r} not found")
        return self._row_art(dict(row))

    def list_articles_admin(self, category_id=None, status=None,
                            limit: int = 200) -> list[HelpArticle]:
        clauses, params = [], []
        if category_id is not None:
            clauses.append("category_id = %s")
            params.append(category_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM help_articles {where} "
                        f"ORDER BY updated_at DESC LIMIT %s", [*params, limit])
            rows = cur.fetchall()
        return [self._row_art(dict(r)) for r in rows]

    def list_published(self, category_id=None,
                       locale=DEFAULT_LOCALE) -> list[HelpArticle]:
        clauses = ["status = 'published'", "locale = %s"]
        params: list = [locale]
        if category_id is not None:
            clauses.append("category_id = %s")
            params.append(category_id)
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM help_articles WHERE {' AND '.join(clauses)} "
                f"ORDER BY sort_order, title", params)
            rows = cur.fetchall()
        return [self._row_art(dict(r)) for r in rows]

    def increment_views(self, art_id: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE help_articles SET view_count = view_count + 1 "
                        "WHERE id = %s", (art_id,))
            conn.commit()

    # ревизии
    def add_revision(self, article_id, title, body_md, editor_admin_id) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO help_article_revisions "
                "(article_id, title, body_md, editor_admin_id) "
                "VALUES (%s, %s, %s, %s)",
                (article_id, title, body_md, editor_admin_id))
            conn.commit()

    def list_revisions(self, article_id, limit: int = 50) -> list[HelpRevision]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM help_article_revisions "
                        "WHERE article_id = %s ORDER BY created_at DESC LIMIT %s",
                        (article_id, limit))
            rows = cur.fetchall()
        return [HelpRevision(**dict(r)) for r in rows]

    # фидбек
    def add_feedback(self, article_id, helpful, comment, voter_hash) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO help_article_feedback "
                "(article_id, helpful, comment, voter_hash) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (article_id, voter_hash) DO NOTHING",
                (article_id, helpful, comment, voter_hash))
            inserted = cur.rowcount > 0
            conn.commit()
        return inserted

    def feedback_stats(self, article_id: str) -> dict:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE helpful), count(*) "
                "FROM help_article_feedback WHERE article_id = %s",
                (article_id,))
            helpful, total = cur.fetchone()
        return {"helpful": int(helpful or 0), "total": int(total or 0)}

    def feedback_summary(self) -> dict:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT article_id, count(*) FILTER (WHERE helpful), count(*) "
                "FROM help_article_feedback GROUP BY article_id")
            return {aid: {"helpful": int(h), "total": int(t)}
                    for aid, h, t in cur.fetchall()}

    def list_feedback_comments(self, limit: int = 100) -> list[dict]:
        # voter_hash сознательно НЕ выбирается
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT article_id, helpful, comment, created_at "
                "FROM help_article_feedback "
                "WHERE comment IS NOT NULL AND comment <> '' "
                "ORDER BY created_at DESC LIMIT %s", (limit,))
            return [{"article_id": aid, "helpful": bool(h), "comment": c,
                     "created_at": ca.isoformat()}
                    for aid, h, c, ca in cur.fetchall()]

    # поиск (HC-5): русская морфология, published-only, ranked;
    # сниппет строит ts_headline с сентинелами [[…]] (не HTML)
    def search_published(self, query, locale=DEFAULT_LOCALE,
                         limit: int = 20) -> list[tuple[HelpArticle, str]]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *, ts_headline(
                    'russian', body_md, plainto_tsquery('russian', %s),
                    'StartSel="[[", StopSel="]]", MaxWords=30, MinWords=10'
                ) AS _snippet
                FROM help_articles
                WHERE status = 'published' AND locale = %s
                  AND search_tsv @@ plainto_tsquery('russian', %s)
                ORDER BY ts_rank(search_tsv, plainto_tsquery('russian', %s)) DESC
                LIMIT %s
                """,
                (query, locale, query, query, limit))
            rows = cur.fetchall()
        out: list[tuple[HelpArticle, str]] = []
        for r in rows:
            d = dict(r)
            snippet = d.pop("_snippet", "") or ""
            out.append((self._row_art(d), snippet))
        return out

    def log_search(self, query: str, results_count: int) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO help_search_log (query, results_count) "
                        "VALUES (%s, %s)", (query[:200], results_count))
            conn.commit()

    def zero_result_queries(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT query, count(*) AS hits, max(created_at) AS last_at "
                "FROM help_search_log WHERE results_count = 0 "
                "GROUP BY query ORDER BY hits DESC, last_at DESC LIMIT %s",
                (limit,))
            return [{"query": q, "hits": int(h), "last_at": la.isoformat()}
                    for q, h, la in cur.fetchall()]

    def top_queries(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT query, count(*) AS hits, max(created_at) AS last_at, "
                "       avg(results_count)::float AS avg_results "
                "FROM help_search_log "
                "GROUP BY query ORDER BY hits DESC, last_at DESC LIMIT %s",
                (limit,))
            return [{"query": q, "hits": int(h), "last_at": la.isoformat(),
                     "avg_results": round(float(ar), 1)}
                    for q, h, la, ar in cur.fetchall()]


# ── Local JSON (dev/tests) ────────────────────────────────────────────────

class LocalFileHelpRegistry(HelpRegistry):
    """Файловый бэкенд: та же семантика; поиск — наивный substring по
    published (морфологии нет — паритет достаточен для тестов API-слоя;
    морфология покрывается integration-тестом на PG в HC-5)."""

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path or os.environ.get(
            "HELP_REGISTRY_PATH",
            str(Path(os.environ.get("ARTIFACTS_DIR", "artifacts")) / "help.json"),
        ))
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self._path.exists():
            return {"categories": {}, "articles": {}, "revisions": [],
                    "feedback": [], "search_log": []}
        return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=_dt))
        tmp.replace(self._path)

    # категории
    def create_category(self, cat: HelpCategory) -> None:
        with self._lock:
            data = self._load()
            if any(c["slug"] == cat.slug and c["locale"] == cat.locale
                   for c in data["categories"].values()):
                raise ValueError(f"category slug {cat.slug!r} already exists")
            data["categories"][cat.id] = asdict(cat)
            self._save(data)

    def get_category(self, cat_id):
        row = self._load()["categories"].get(cat_id)
        return _cat(row) if row else None

    def get_category_by_slug(self, slug, locale=DEFAULT_LOCALE):
        for row in self._load()["categories"].values():
            if row["slug"] == slug and row["locale"] == locale:
                return _cat(row)
        return None

    def update_category(self, cat_id, **fields):
        self._check_fields(fields, _CATEGORY_MUTABLE)
        with self._lock:
            data = self._load()
            row = data["categories"].get(cat_id)
            if row is None:
                raise KeyError(f"help category {cat_id!r} not found")
            row.update({k: _dt(v) if isinstance(v, datetime) else v
                        for k, v in fields.items()})
            row["updated_at"] = _dt(_now())
            self._save(data)
            return _cat(row)

    def list_categories(self, locale=DEFAULT_LOCALE):
        cats = [_cat(r) for r in self._load()["categories"].values()
                if r["locale"] == locale]
        cats.sort(key=lambda c: (c.sort_order, c.title))
        return cats

    # статьи
    def create_article(self, art: HelpArticle) -> None:
        with self._lock:
            data = self._load()
            if any(a["slug"] == art.slug and a["locale"] == art.locale
                   for a in data["articles"].values()):
                raise ValueError(f"article slug {art.slug!r} already exists")
            data["articles"][art.id] = asdict(art)
            self._save(data)

    def get_article(self, art_id):
        row = self._load()["articles"].get(art_id)
        return _art(row) if row else None

    def get_article_by_slug(self, slug, locale=DEFAULT_LOCALE):
        for row in self._load()["articles"].values():
            if row["slug"] == slug and row["locale"] == locale:
                return _art(row)
        return None

    def update_article(self, art_id, **fields):
        self._check_fields(fields, _ARTICLE_MUTABLE)
        with self._lock:
            data = self._load()
            row = data["articles"].get(art_id)
            if row is None:
                raise KeyError(f"help article {art_id!r} not found")
            row.update({k: _dt(v) if isinstance(v, datetime) else v
                        for k, v in fields.items()})
            row["updated_at"] = _dt(_now())
            self._save(data)
            return _art(row)

    def list_articles_admin(self, category_id=None, status=None, limit=200):
        arts = [_art(r) for r in self._load()["articles"].values()]
        if category_id is not None:
            arts = [a for a in arts if a.category_id == category_id]
        if status is not None:
            arts = [a for a in arts if a.status == status]
        arts.sort(key=lambda a: a.updated_at, reverse=True)
        return arts[:limit]

    def list_published(self, category_id=None, locale=DEFAULT_LOCALE):
        arts = [a for a in
                (_art(r) for r in self._load()["articles"].values())
                if a.status == PUBLISHED and a.locale == locale]
        if category_id is not None:
            arts = [a for a in arts if a.category_id == category_id]
        arts.sort(key=lambda a: (a.sort_order, a.title))
        return arts

    def increment_views(self, art_id: str) -> None:
        with self._lock:
            data = self._load()
            if art_id in data["articles"]:
                data["articles"][art_id]["view_count"] += 1
                self._save(data)

    # ревизии
    def add_revision(self, article_id, title, body_md, editor_admin_id):
        with self._lock:
            data = self._load()
            data["revisions"].append({
                "id": len(data["revisions"]) + 1, "article_id": article_id,
                "title": title, "body_md": body_md,
                "editor_admin_id": editor_admin_id, "created_at": _dt(_now())})
            self._save(data)

    def list_revisions(self, article_id, limit=50):
        revs = [r for r in self._load()["revisions"]
                if r["article_id"] == article_id]
        revs.sort(key=lambda r: r["created_at"], reverse=True)
        return [HelpRevision(**{**r, "created_at": _pdt(r["created_at"])})
                for r in revs[:limit]]

    # фидбек
    def add_feedback(self, article_id, helpful, comment, voter_hash) -> bool:
        with self._lock:
            data = self._load()
            if any(f["article_id"] == article_id and f["voter_hash"] == voter_hash
                   for f in data["feedback"]):
                return False
            data["feedback"].append({
                "article_id": article_id, "helpful": helpful,
                "comment": comment, "voter_hash": voter_hash,
                "created_at": _dt(_now())})
            self._save(data)
            return True

    def feedback_stats(self, article_id: str) -> dict:
        rows = [f for f in self._load()["feedback"]
                if f["article_id"] == article_id]
        return {"helpful": sum(1 for f in rows if f["helpful"]),
                "total": len(rows)}

    def feedback_summary(self) -> dict:
        agg: dict = {}
        for f in self._load()["feedback"]:
            z = agg.setdefault(f["article_id"], {"helpful": 0, "total": 0})
            z["total"] += 1
            if f["helpful"]:
                z["helpful"] += 1
        return agg

    def list_feedback_comments(self, limit: int = 100) -> list[dict]:
        # voter_hash сознательно НЕ отдаётся
        rows = [f for f in self._load()["feedback"] if f.get("comment")]
        rows.sort(key=lambda f: f["created_at"], reverse=True)
        return [{"article_id": f["article_id"], "helpful": f["helpful"],
                 "comment": f["comment"], "created_at": f["created_at"]}
                for f in rows[:limit]]

    # поиск: наивный substring + сниппет-окно вокруг первого совпадения
    # (сентинелы [[…]] — паритет контракта с PG ts_headline)
    def search_published(self, query, locale=DEFAULT_LOCALE, limit=20):
        q = query.lower()
        out: list[tuple[HelpArticle, str]] = []
        for a in self.list_published(locale=locale):
            src = a.body_md if q in a.body_md.lower() else (
                a.title if q in a.title.lower() else None)
            if src is None:
                continue
            i = src.lower().index(q)
            start = max(0, i - 80)
            end = min(len(src), i + len(q) + 80)
            snippet = (src[start:i] + "[[" + src[i:i + len(q)] + "]]"
                       + src[i + len(q):end])
            out.append((a, snippet))
            if len(out) >= limit:
                break
        return out

    def log_search(self, query, results_count) -> None:
        with self._lock:
            data = self._load()
            data["search_log"].append({
                "query": query[:200], "results_count": results_count,
                "created_at": _dt(_now())})
            self._save(data)

    def zero_result_queries(self, limit=50):
        zeros: dict = {}
        for r in self._load()["search_log"]:
            if r["results_count"] == 0:
                z = zeros.setdefault(r["query"], {"hits": 0, "last_at": ""})
                z["hits"] += 1
                z["last_at"] = max(z["last_at"], r["created_at"])
        out = [{"query": q, **v} for q, v in zeros.items()]
        out.sort(key=lambda x: (-x["hits"], x["last_at"]), reverse=False)
        return out[:limit]

    def top_queries(self, limit=50):
        agg: dict = {}
        for r in self._load()["search_log"]:
            z = agg.setdefault(r["query"],
                               {"hits": 0, "last_at": "", "_sum": 0})
            z["hits"] += 1
            z["_sum"] += r["results_count"]
            z["last_at"] = max(z["last_at"], r["created_at"])
        out = [{"query": q, "hits": v["hits"], "last_at": v["last_at"],
                "avg_results": round(v["_sum"] / v["hits"], 1)}
               for q, v in agg.items()]
        out.sort(key=lambda x: (-x["hits"], x["last_at"]), reverse=False)
        return out[:limit]


def _dt(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _pdt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


def _cat(row: dict) -> HelpCategory:
    kw = dict(row)
    for k in ("created_at", "updated_at"):
        kw[k] = _pdt(kw.get(k))
    return HelpCategory(**kw)


def _art(row: dict) -> HelpArticle:
    kw = dict(row)
    for k in ("published_at", "created_at", "updated_at"):
        kw[k] = _pdt(kw.get(k))
    return HelpArticle(**kw)


# ── фабрика ──────────────────────────────────────────────────────────────

_registry: Optional[HelpRegistry] = None
_registry_lock = threading.Lock()


def get_help_registry() -> HelpRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            url = os.environ.get("DATABASE_URL")
            _registry = (PostgresHelpRegistry(url) if url
                         else LocalFileHelpRegistry())
        return _registry


def reset_registry_for_tests() -> None:
    global _registry
    with _registry_lock:
        _registry = None
