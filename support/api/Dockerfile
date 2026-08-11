FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# все python-модули сервиса разом — добавление нового модуля больше не
# требует править COPY (класс инцидентов #607/#609: недокопированный
# модуль; проба import ниже ловит остальное)
COPY *.py ./
# #567: build-time проба импорта — недокопированный модуль валит билд,
# а не прод-контейнер (инцидент 2026-08-06: logic.py не попал в COPY).
RUN VECTORDB_PASSWORD=build-probe python -c "import app"
ENV HF_HOME=/models/hf-cache
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8090/support/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
