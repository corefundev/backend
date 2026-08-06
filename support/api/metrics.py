"""#509: метрики бота в формате Prometheus + лимитер TG-эскалаций.

Экспортер самописный и минимальный (счётчики + гистограммы, text
format 0.0.4) — осознанно БЕЗ prometheus_client: supply-chain правило
проекта, зависимость ради 40 строк не нужна. Процесс один (uvicorn,
async) — без блокировок.
"""
from __future__ import annotations

import time

_BUCKETS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 90.0)


class Registry:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._hist: dict[str, list[float]] = {}
        self._hist_sum: dict[str, float] = {}
        self._hist_cnt: dict[str, int] = {}

    def inc(self, name: str, labels: dict[str, str] | None = None,
            value: float = 1.0) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, seconds: float) -> None:
        if name not in self._hist:
            self._hist[name] = [0.0] * (len(_BUCKETS) + 1)
            self._hist_sum[name] = 0.0
            self._hist_cnt[name] = 0
        buckets = self._hist[name]
        for i, edge in enumerate(_BUCKETS):
            if seconds <= edge:
                buckets[i] += 1
        buckets[-1] += 1  # +Inf
        self._hist_sum[name] += seconds
        self._hist_cnt[name] += 1

    def render(self) -> str:
        out: list[str] = []
        for (name, labels), val in sorted(self._counters.items()):
            lbl = ",".join(f'{k}="{v}"' for k, v in labels)
            out.append(f"{name}{{{lbl}}} {val}" if lbl else f"{name} {val}")
        for name, buckets in sorted(self._hist.items()):
            for i, edge in enumerate(_BUCKETS):
                out.append(f'{name}_bucket{{le="{edge}"}} {buckets[i]}')
            out.append(f'{name}_bucket{{le="+Inf"}} {buckets[-1]}')
            out.append(f"{name}_sum {self._hist_sum[name]}")
            out.append(f"{name}_count {self._hist_cnt[name]}")
        return "\n".join(out) + "\n"


class EscalationNotifyLimiter:
    """TG-уведомления об эскалациях без спама: не чаще одного на сессию
    за session_window и не больше hourly_cap в час (скользящее окно)."""

    def __init__(self, session_window: float = 600.0,
                 hourly_cap: int = 20) -> None:
        self._session_window = session_window
        self._hourly_cap = hourly_cap
        self._last_by_session: dict[str, float] = {}
        self._sent_at: list[float] = []

    def allow(self, session_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._last_by_session.get(session_id)
        if last is not None and now - last < self._session_window:
            return False
        self._sent_at = [t for t in self._sent_at if now - t < 3600.0]
        if len(self._sent_at) >= self._hourly_cap:
            return False
        self._last_by_session[session_id] = now
        self._sent_at.append(now)
        # гигиена: не копим сессии бесконечно
        if len(self._last_by_session) > 5000:
            cutoff = now - self._session_window
            self._last_by_session = {
                s: t for s, t in self._last_by_session.items() if t > cutoff}
        return True
