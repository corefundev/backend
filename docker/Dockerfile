# ══════════════════════════════════════════════════════
# SKU Forecasting Platform — API Image
# Python 3.11-slim, non-root user, healthcheck
# ══════════════════════════════════════════════════════
FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

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
# Re-fix ПОСЛЕ requirements: транзитивные зависимости откатывают setuptools
# (Trivy: CVE-2025-47273, path traversal в PackageIndex ≤78.1.0) — финальный
# апгрейд гарантирует патченную версию в итоговом слое образа.
RUN pip install --no-cache-dir --upgrade "setuptools>=80"

# Security (class-elimination): pip — build-инструмент, в рантайме он не
# нужен (аудит: entrypoints uvicorn/rq/скрипты, ни одного вызова pip в
# src/scripts/compose). Его вендорённые копии (pip/_vendor: msgpack,
# setuptools, …) регулярно ловят CVE и валят image-скан, хотя в НАШЕМ
# дереве зависимостей стоят патченные версии. Удаляем pip целиком —
# уязвимый код физически отсутствует в образе, а не спрятан от сканера.
# setuptools ОСТАЁТСЯ (патченный ≥80): pkg_resources нужен рантайму
# сторонних библиотек.
# #605: CVE-2026-69247 закрыт только в cryptography 50.0.0, а mlflow
# 3.15.1 держит потолок <50 — резолвер не пустит 50.x через requirements.
# Осознанный security-override (--no-deps: только сам пакет) + проба
# импортов: билд падает, если override ломает потребителей.
RUN pip install --no-cache-dir --no-deps "cryptography==50.0.0" && \
    python -c "import cryptography, jwt, mlflow; import cryptography.hazmat.primitives.asymmetric.rsa"

# #509/Trivy 2026-08-11: linux-libc-dev (kernel headers) — артефакт
# сборочного тулчейна gcc/g++; в рантайме не нужен, а его свежие CVE
# (патч ещё не в зеркалах) валят гейт. Убираем из ИТОГОВОЙ ФС каскадом
# (libc6-dev/g++ уходят следом — после pip install ничего не компилируется).
# Рантайм-библиотеки прикалываем явно: libgomp1 (lightgbm), libpq5 (psycopg2).
RUN apt-mark manual libgomp1 libpq5 2>/dev/null; \
    apt-get purge -y linux-libc-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip uninstall -y pip


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
