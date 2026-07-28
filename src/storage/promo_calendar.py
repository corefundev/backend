"""
src/storage/promo_calendar.py — #570 PC-1: реестр «Календаря акций».

Календарь — справочник known-future промо per-dataset (вариант B, решение
владельца). Жизненный цикл: загрузка создаёт КАНДИДАТА (pending_review,
события пишутся сразу — превью и apply не перечитывают файл), явное
«Применить» делает atomic swap: прежний active → replaced, кандидат →
active — одна транзакция, датасет ни мгновения не живёт с половиной
календаря. Инвариант «≤1 active на датасет» дублируется частичным
уникальным индексом в БД (037_promo_calendar.sql).

Fail-open контракт (#570): нет активного календаря → нулевые промо-фичи,
поведение прогноза как раньше. Сломанный реестр — НЕ «молча нулевые»:
исключение поднимается наверх, решает вызывающий слой.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STATUSES = ("pending_review", "active", "replaced", "discarded")


@dataclass
class PromoEvent:
    sku: Optional[str]
    category: Optional[str]
    date_from: str          # ISO YYYY-MM-DD
    date_to: str
    depth_pct: Optional[float] = None
    name: Optional[str] = None


@dataclass
class PromoCalendarRecord:
    calendar_id: str
    client_id: str
    dataset_id: str
    filename: str
    status: str
    report: dict = field(default_factory=dict)
    rows_accepted: int = 0
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    source_key: Optional[str] = None
    uploaded_at: Optional[str] = None
    applied_at: Optional[str] = None
    replaced_at: Optional[str] = None


def _new_id() -> str:
    return f"pcal_{uuid.uuid4().hex[:16]}"


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


class PostgresPromoCalendarRegistry:
    """Postgres-реестр (образец — src/storage/datasets.py)."""

    def __init__(self, database_url: str):
        import psycopg2
        self._psycopg2 = psycopg2
        self._dsn = database_url

    def _conn(self):
        return self._psycopg2.connect(self._dsn)

    _REC_COLS = ("calendar_id, client_id, dataset_id, filename, status, "
                 "report, rows_accepted, date_min, date_max, source_key, "
                 "uploaded_at, applied_at, replaced_at")

    def _row_to_rec(self, row) -> PromoCalendarRecord:
        return PromoCalendarRecord(
            calendar_id=row[0], client_id=row[1], dataset_id=row[2],
            filename=row[3], status=row[4],
            report=row[5] if isinstance(row[5], dict) else json.loads(row[5] or "{}"),
            rows_accepted=int(row[6] or 0),
            date_min=_iso(row[7]), date_max=_iso(row[8]), source_key=row[9],
            uploaded_at=_iso(row[10]), applied_at=_iso(row[11]),
            replaced_at=_iso(row[12]),
        )

    def create_candidate(
        self, client_id: str, dataset_id: str, filename: str,
        report: dict, events: "list[PromoEvent]", source_key: Optional[str],
    ) -> PromoCalendarRecord:
        """Кандидат + события одной транзакцией; прежние кандидаты датасета
        отбрасываются (актуален только последний загруженный файл)."""
        cal_id = _new_id()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sku_promo_calendars SET status = 'discarded' "
                "WHERE dataset_id = %s AND status = 'pending_review'",
                (dataset_id,))
            cur.execute(
                "INSERT INTO sku_promo_calendars (calendar_id, client_id, "
                "dataset_id, filename, status, report, rows_accepted, "
                "date_min, date_max, source_key) "
                "VALUES (%s, %s, %s, %s, 'pending_review', %s, %s, %s, %s, %s)",
                (cal_id, client_id, dataset_id, filename, json.dumps(report),
                 len(events), report.get("date_min"), report.get("date_max"),
                 source_key))
            for e in events:
                cur.execute(
                    "INSERT INTO sku_promo_events (calendar_id, client_id, "
                    "dataset_id, sku, category, date_from, date_to, "
                    "depth_pct, name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (cal_id, client_id, dataset_id, e.sku, e.category,
                     e.date_from, e.date_to, e.depth_pct, e.name))
            conn.commit()
        return self.get(cal_id)  # перечитываем — с серверными timestamp'ами

    def get(self, calendar_id: str) -> Optional[PromoCalendarRecord]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._REC_COLS} FROM sku_promo_calendars "
                "WHERE calendar_id = %s", (calendar_id,))
            row = cur.fetchone()
        return self._row_to_rec(row) if row else None

    def _get_by_status(self, dataset_id: str, status: str) -> Optional[PromoCalendarRecord]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._REC_COLS} FROM sku_promo_calendars "
                "WHERE dataset_id = %s AND status = %s "
                "ORDER BY uploaded_at DESC LIMIT 1", (dataset_id, status))
            row = cur.fetchone()
        return self._row_to_rec(row) if row else None

    def get_active(self, dataset_id: str) -> Optional[PromoCalendarRecord]:
        return self._get_by_status(dataset_id, "active")

    def get_candidate(self, dataset_id: str) -> Optional[PromoCalendarRecord]:
        return self._get_by_status(dataset_id, "pending_review")

    def apply(self, calendar_id: str) -> PromoCalendarRecord:
        """Atomic swap: active→replaced, кандидат→active. Одна транзакция."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT dataset_id, status FROM sku_promo_calendars "
                "WHERE calendar_id = %s FOR UPDATE", (calendar_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"calendar {calendar_id!r} not found")
            dataset_id, status = row
            if status != "pending_review":
                raise ValueError(f"calendar {calendar_id!r} is {status}, "
                                 "not pending_review")
            cur.execute(
                "UPDATE sku_promo_calendars SET status = 'replaced', "
                "replaced_at = now() WHERE dataset_id = %s AND status = 'active'",
                (dataset_id,))
            cur.execute(
                "UPDATE sku_promo_calendars SET status = 'active', "
                "applied_at = now() WHERE calendar_id = %s", (calendar_id,))
            conn.commit()
        return self.get(calendar_id)

    def remove_active(self, dataset_id: str) -> bool:
        """Снять активный календарь (fail-open к «календаря нет»)."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sku_promo_calendars SET status = 'replaced', "
                "replaced_at = now() WHERE dataset_id = %s AND status = 'active'",
                (dataset_id,))
            hit = cur.rowcount > 0
            conn.commit()
        return hit

    def list_events(self, calendar_id: str) -> "list[PromoEvent]":
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT sku, category, date_from, date_to, depth_pct, name "
                "FROM sku_promo_events WHERE calendar_id = %s ORDER BY id",
                (calendar_id,))
            rows = cur.fetchall()
        return [PromoEvent(sku=r[0], category=r[1], date_from=_iso(r[2]),
                           date_to=_iso(r[3]),
                           depth_pct=None if r[4] is None else float(r[4]),
                           name=r[5]) for r in rows]


class LocalFilePromoCalendarRegistry:
    """JSON-файловый реестр для dev/тестов (образец — datasets.py)."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self._path.is_file():
            return {"calendars": {}, "events": {}}
        return json.loads(self._path.read_text() or '{"calendars":{},"events":{}}')

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str))
        tmp.replace(self._path)

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

    def create_candidate(self, client_id, dataset_id, filename, report,
                         events, source_key) -> PromoCalendarRecord:
        with self._lock:
            data = self._load()
            for c in data["calendars"].values():
                if c["dataset_id"] == dataset_id and c["status"] == "pending_review":
                    c["status"] = "discarded"
            rec = PromoCalendarRecord(
                calendar_id=_new_id(), client_id=client_id,
                dataset_id=dataset_id, filename=filename,
                status="pending_review", report=report,
                rows_accepted=len(events),
                date_min=report.get("date_min"), date_max=report.get("date_max"),
                source_key=source_key, uploaded_at=self._now())
            data["calendars"][rec.calendar_id] = asdict(rec)
            data["events"][rec.calendar_id] = [asdict(e) for e in events]
            self._save(data)
            return rec

    def get(self, calendar_id) -> Optional[PromoCalendarRecord]:
        c = self._load()["calendars"].get(calendar_id)
        return PromoCalendarRecord(**c) if c else None

    def _get_by_status(self, dataset_id, status) -> Optional[PromoCalendarRecord]:
        cands = [c for c in self._load()["calendars"].values()
                 if c["dataset_id"] == dataset_id and c["status"] == status]
        cands.sort(key=lambda c: c.get("uploaded_at") or "", reverse=True)
        return PromoCalendarRecord(**cands[0]) if cands else None

    def get_active(self, dataset_id):
        return self._get_by_status(dataset_id, "active")

    def get_candidate(self, dataset_id):
        return self._get_by_status(dataset_id, "pending_review")

    def apply(self, calendar_id) -> PromoCalendarRecord:
        with self._lock:
            data = self._load()
            c = data["calendars"].get(calendar_id)
            if c is None:
                raise KeyError(f"calendar {calendar_id!r} not found")
            if c["status"] != "pending_review":
                raise ValueError(
                    f"calendar {calendar_id!r} is {c['status']}, not pending_review")
            for other in data["calendars"].values():
                if (other["dataset_id"] == c["dataset_id"]
                        and other["status"] == "active"):
                    other["status"] = "replaced"
                    other["replaced_at"] = self._now()
            c["status"] = "active"
            c["applied_at"] = self._now()
            self._save(data)
            return PromoCalendarRecord(**c)

    def remove_active(self, dataset_id) -> bool:
        with self._lock:
            data = self._load()
            hit = False
            for c in data["calendars"].values():
                if c["dataset_id"] == dataset_id and c["status"] == "active":
                    c["status"] = "replaced"
                    c["replaced_at"] = self._now()
                    hit = True
            if hit:
                self._save(data)
            return hit

    def list_events(self, calendar_id) -> "list[PromoEvent]":
        return [PromoEvent(**e)
                for e in self._load()["events"].get(calendar_id, [])]


_registry = None
_registry_lock = threading.Lock()


def get_promo_calendar_registry():
    """Postgres при DATABASE_URL, иначе локальный JSON (dev/тесты) — та же
    развилка, что у остальных реестров."""
    global _registry
    with _registry_lock:
        if _registry is None:
            dsn = os.environ.get("DATABASE_URL")
            if dsn:
                _registry = PostgresPromoCalendarRegistry(dsn)
            else:
                base = os.environ.get("ARTIFACTS_DIR", "artifacts")
                _registry = LocalFilePromoCalendarRegistry(
                    str(Path(base) / "_registry" / "promo_calendars.json"))
        return _registry


def reset_registry_for_tests() -> None:
    global _registry
    with _registry_lock:
        _registry = None
