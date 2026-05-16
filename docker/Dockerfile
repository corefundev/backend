# ══════════════════════════════════════════════════════
# SKU Forecasting Platform — API Image
# Python 3.11-slim, non-root user, healthcheck
# ══════════════════════════════════════════════════════
FROM python:3.11-slim

# Непривилегированный пользователь
RUN groupadd -r sku && useradd -r -g sku -d /app -s /sbin/nologin sku

WORKDIR /app

# Системные зависимости
# Audit Trivy 2026-05-16 — `apt-get upgrade -y` pulls debian security
# patches at build time. Without it, the floating python:3.11-slim tag
# inherits whatever was current at upstream rebuild — which may lag
# behind freshly-disclosed CVEs in debian packages. Trivy flagged this
# gap on fe8294d / 0120084; the upgrade fixes it without changing the
# base tag.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        gcc g++ libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Bake-fix: python:3.11-slim ships pip + setuptools + wheel; setuptools
# VENDORS jaraco.context inside _vendor/ and pip vendors wheel — Trivy
# scans those METADATA files and flags CVE-2026-23949 (jaraco path
# traversal) + CVE-2026-24049 (wheel priv-esc) even after a top-level
# upgrade. Bumping setuptools to ≥80 / pip to latest replaces the
# vendored copies with patched ones (current setuptools vendors
# jaraco.context 6.x; current pip vendors wheel 0.46+).
RUN pip install --no-cache-dir --upgrade \
        pip \
        "setuptools>=80" \
        wheel==0.46.2 \
        jaraco.context==6.1.0

# Зависимости Python (отдельный слой — кэшируется)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip uninstall -y multipart && pip install --no-cache-dir python-multipart

# Исходный код
COPY src/ src/
COPY configs/ configs/
COPY scripts/ scripts/
COPY migrations/ migrations/

# Права
RUN chown -R sku:sku /app
USER sku

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "src.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info"]
