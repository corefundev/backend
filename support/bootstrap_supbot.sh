#!/bin/bash
# SUP-1 bootstrap на чистом supbot-VPS. Идемпотентен.
set -euo pipefail
mkdir -p /srv/supbot/kb
cd /srv/supbot
if [ ! -f .env ]; then
    umask 077
    echo "VECTORDB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')" > .env
    echo ".env создан (chmod 600)"
fi
docker compose --env-file .env -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps
if [ ! -d venv ]; then python3 -m venv venv; fi
./venv/bin/pip install -q -r ingest/requirements.txt
echo "bootstrap OK; запуск инжеста:"
echo "  set -a; . /srv/supbot/.env; set +a; ./venv/bin/python ingest/ingest.py --kb-dir ./kb"
