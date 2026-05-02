# ══════════════════════════════════════════════════════
# SKU Forecasting Platform — API Image
# Python 3.11-slim, non-root user, healthcheck
# ══════════════════════════════════════════════════════
FROM python:3.11-slim

# Непривилегированный пользователь
RUN groupadd -r sku && useradd -r -g sku -d /app -s /sbin/nologin sku

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

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
