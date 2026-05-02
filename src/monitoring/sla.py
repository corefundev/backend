"""
src/monitoring/sla.py

SLA monitoring и error budget tracking.

SLOs (Service Level Objectives):
  - latency_p99_ms < 200        → latency SLO
  - error_rate < 0.001          → availability SLO (99.9%)
  - uptime >= 0.995             → uptime SLO (99.5%)

Error budget:
  - Месячный бюджет = допустимые минуты простоя при SLO 99.5%
  - Если потрачено > 80% бюджета → заморозить деплои
  - Алерт при > 50% и > 80% трат бюджета
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SLOConfig:
    latency_p99_ms:  float = 200.0    # p99 latency target
    error_rate_max:  float = 0.001    # 0.1% max error rate
    uptime_target:   float = 0.995    # 99.5% uptime
    window_seconds:  int   = 3600     # rolling 1h window for metrics


@dataclass
class SLOStatus:
    client_id:          str
    timestamp:          str
    latency_p99_ms:     float
    error_rate:         float
    latency_ok:         bool
    availability_ok:    bool
    error_budget_pct:   float   # how much of monthly budget is spent
    budget_frozen:      bool    # True = freeze deployments
    n_requests:         int
    n_errors:           int


class SLAMonitor:
    """
    Per-client SLA monitor with rolling window metrics and error budget.
    Thread-safe via deque.
    """

    MONTHLY_MINUTES = 30 * 24 * 60   # minutes in a month

    def __init__(self, client_id: str, config: Optional[SLOConfig] = None):
        self.client_id    = client_id
        self.slo          = config or SLOConfig()
        self._latencies:  deque = deque(maxlen=10_000)
        self._timestamps: deque = deque(maxlen=10_000)
        self._errors:     deque = deque(maxlen=10_000)
        self._budget_used_minutes: float = 0.0   # accumulated this month
        self._last_check: float = time.time()

    # ── Recording ─────────────────────────────────────────────

    def record_request(self, latency_ms: float, success: bool) -> None:
        """Record one API request."""
        now = time.time()
        self._latencies.append(latency_ms)
        self._timestamps.append(now)
        self._errors.append(0 if success else 1)

        # Evict old samples outside rolling window
        cutoff = now - self.slo.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
            self._latencies.popleft()
            self._errors.popleft()

    # ── Metrics ───────────────────────────────────────────────

    @property
    def p99_latency_ms(self) -> float:
        if not self._latencies:
            return 0.0
        lats = sorted(self._latencies)
        idx  = max(0, int(len(lats) * 0.99) - 1)
        return float(lats[idx])

    @property
    def error_rate(self) -> float:
        if not self._errors:
            return 0.0
        return float(sum(self._errors)) / len(self._errors)

    @property
    def n_requests(self) -> int:
        return len(self._latencies)

    @property
    def n_errors(self) -> int:
        return int(sum(self._errors))

    # ── Error budget ──────────────────────────────────────────

    def update_budget(self, downtime_minutes: float) -> None:
        """Accumulate downtime into monthly error budget."""
        self._budget_used_minutes += downtime_minutes

    @property
    def error_budget_allowed_minutes(self) -> float:
        """Monthly error budget in minutes: (1 - uptime_target) * month_minutes."""
        return (1 - self.slo.uptime_target) * self.MONTHLY_MINUTES

    @property
    def error_budget_pct(self) -> float:
        """Fraction of monthly error budget spent (0.0 – 1.0+)."""
        budget = self.error_budget_allowed_minutes
        return self._budget_used_minutes / budget if budget > 0 else 0.0

    @property
    def budget_frozen(self) -> bool:
        """True when > 80% of error budget spent → freeze deployments."""
        return self.error_budget_pct > 0.80

    # ── Status ────────────────────────────────────────────────

    def get_status(self) -> SLOStatus:
        p99      = self.p99_latency_ms
        err_rate = self.error_rate
        status = SLOStatus(
            client_id       = self.client_id,
            timestamp       = datetime.now(timezone.utc).isoformat(),
            latency_p99_ms  = round(p99, 1),
            error_rate      = round(err_rate, 5),
            latency_ok      = p99 <= self.slo.latency_p99_ms,
            availability_ok = err_rate <= self.slo.error_rate_max,
            error_budget_pct = round(self.error_budget_pct * 100, 1),
            budget_frozen   = self.budget_frozen,
            n_requests      = self.n_requests,
            n_errors        = self.n_errors,
        )

        # Log alerts
        if not status.latency_ok:
            logger.warning(
                f"SLO BREACH [{self.client_id}]: "
                f"p99={p99:.0f}ms > {self.slo.latency_p99_ms:.0f}ms target"
            )
        if not status.availability_ok:
            logger.warning(
                f"SLO BREACH [{self.client_id}]: "
                f"error_rate={err_rate:.3%} > {self.slo.error_rate_max:.3%} target"
            )
        if self.budget_frozen:
            logger.error(
                f"ERROR BUDGET EXHAUSTED [{self.client_id}]: "
                f"{self.error_budget_pct:.0%} of monthly budget spent — "
                f"freeze deployments!"
            )
        elif self.error_budget_pct > 0.50:
            logger.warning(
                f"Error budget [{self.client_id}]: "
                f"{self.error_budget_pct:.0%} spent (>50% alert)"
            )

        return status

    def to_prometheus_metrics(self) -> str:
        """Format as Prometheus exposition text."""
        s = self.get_status()
        lines = [
            f'sla_latency_p99_ms{{client_id="{self.client_id}"}} {s.latency_p99_ms}',
            f'sla_error_rate{{client_id="{self.client_id}"}} {s.error_rate}',
            f'sla_error_budget_pct{{client_id="{self.client_id}"}} {s.error_budget_pct}',
            f'sla_latency_ok{{client_id="{self.client_id}"}} {int(s.latency_ok)}',
            f'sla_availability_ok{{client_id="{self.client_id}"}} {int(s.availability_ok)}',
            f'sla_budget_frozen{{client_id="{self.client_id}"}} {int(s.budget_frozen)}',
            f'sla_n_requests{{client_id="{self.client_id}"}} {s.n_requests}',
        ]
        return "\n".join(lines)


# ── Global registry ───────────────────────────────────────────

_monitors: dict[str, SLAMonitor] = {}


def get_monitor(client_id: str) -> SLAMonitor:
    """Get or create SLAMonitor for a client."""
    if client_id not in _monitors:
        _monitors[client_id] = SLAMonitor(client_id)
    return _monitors[client_id]


def record_api_call(
    client_id:   str,
    latency_ms:  float,
    success:     bool,
) -> None:
    """Convenience: record one API call into the client's SLA monitor."""
    get_monitor(client_id).record_request(latency_ms, success)


def get_all_sla_status() -> list[SLOStatus]:
    """Return SLO status for all clients."""
    return [m.get_status() for m in _monitors.values()]


def all_prometheus_metrics() -> str:
    """Return Prometheus metrics for all clients."""
    return "\n".join(m.to_prometheus_metrics() for m in _monitors.values())
