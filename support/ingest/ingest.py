"""SUP-1 (#504): инжест корпуса БЗ в pgvector.

Источники (порядок = приоритет правды):
  1. ЖИВЫЕ опубликованные статьи Помощи + Новости (публичный API
     api.sprosly.com — то, что реально видит клиент);
  2. Курируемый корпус docs/support-kb/*.md (выгружается вместе со
     скриптом; служебные _*.md пропускаются);
  3. Тарифы из GET /plans (лимиты — единственный источник правды).

Чанкинг: по заголовкам ##, длинные секции режутся ~1200 символов с
перекрытием 150. Эмбеддинги: intfloat/multilingual-e5-small (384d,
CPU; префиксы passage:/query: по контракту e5). Инкрементальность —
по sha256 чанка: неизменённые не переэмбеддятся.

Запуск (на supbot-VPS):
    VECTORDB_PASSWORD=... python3 ingest.py --kb-dir ./kb
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg2
import requests

API = os.environ.get("SPROSLY_API", "https://api.sprosly.com")
EMBED_MODEL = "intfloat/multilingual-e5-small"
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150


def fetch_live_docs() -> list[dict]:
    docs: list[dict] = []
    cats = requests.get(f"{API}/help/categories", timeout=30).json()["categories"]
    for c in cats:
        cat = requests.get(f"{API}/help/categories/{c['slug']}", timeout=30).json()
        for a in cat.get("articles", []):
            art = requests.get(f"{API}/help/articles/{a['slug']}", timeout=30).json()
            docs.append({
                "doc_id": f"help:{art['slug']}", "source": "help",
                "title": art["title"], "url_path": f"/help/a/{art['slug']}",
                "text": _html_to_text(art.get("body_html") or art.get("body_md") or ""),
            })
    news = requests.get(f"{API}/news", timeout=30).json()
    for p in (news.get("posts") if isinstance(news, dict) else news) or []:
        post = requests.get(f"{API}/news/{p['slug']}", timeout=30).json()
        docs.append({
            "doc_id": f"news:{post['slug']}", "source": "news",
            "title": post["title"], "url_path": f"/news/{post['slug']}",
            "text": _html_to_text(post.get("body_html") or post.get("body_md") or ""),
        })
    plans = requests.get(f"{API}/plans", timeout=30).json()["plans"]
    lines = ["Тарифы Sprosly и их лимиты:"]
    for p in plans:
        lines.append(
            f"- {p['display_name']} (модель {p['model_display_name']}): "
            f"до {p['max_skus'] or 'неограниченно'} SKU, горизонт "
            f"{p['max_horizon_days']} дней, датасетов {p.get('datasets_limit')}, "
            f"пауза между обучениями "
            f"{str(p['training_cooldown_hours']) + ' ч' if p['training_cooldown_hours'] else 'нет'}.")
    docs.append({"doc_id": "plans:limits", "source": "plans",
                 "title": "Тарифы и лимиты", "url_path": "/plans",
                 "text": "\n".join(lines)})
    return docs


def _html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def load_kb_files(kb_dir: Path) -> list[dict]:
    docs = []
    for f in sorted(kb_dir.glob("*.md")):
        if f.name.startswith("_"):
            continue
        docs.append({"doc_id": f"kb:{f.stem}", "source": "kb-file",
                     "title": f.stem.replace("--", " / ").replace("-", " "),
                     "url_path": None, "text": f.read_text(encoding="utf-8")})
    return docs


def chunk(text: str) -> list[str]:
    sections = re.split(r"(?m)^##\s+", text)
    out: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        while len(sec) > CHUNK_CHARS:
            out.append(sec[:CHUNK_CHARS])
            sec = sec[CHUNK_CHARS - CHUNK_OVERLAP:]
        out.append(sec)
    return [c for c in out if len(c) > 40]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-dir", type=Path, default=Path("kb"))
    ap.add_argument("--db", default="postgresql://supbot:{pw}@127.0.0.1:5433/supbot")
    args = ap.parse_args()
    pw = os.environ.get("VECTORDB_PASSWORD")
    if not pw:
        sys.exit("VECTORDB_PASSWORD is required")

    docs = fetch_live_docs()
    if args.kb_dir.is_dir():
        docs += load_kb_files(args.kb_dir)
    print(f"corpus: {len(docs)} docs")

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(EMBED_MODEL, device="cpu")

    conn = psycopg2.connect(args.db.format(pw=pw))
    cur = conn.cursor()
    cur.execute(Path(__file__).with_name("schema.sql").read_text())
    cur.execute("INSERT INTO kb_ingest_runs (docs) VALUES (%s) RETURNING id", (len(docs),))
    run_id = cur.fetchone()[0]

    total, skipped = 0, 0
    for d in docs:
        pieces = chunk(d["text"])
        seen_ids = []
        for i, piece in enumerate(pieces):
            sha = hashlib.sha256(piece.encode()).hexdigest()
            seen_ids.append(i)
            cur.execute("SELECT content_sha FROM kb_chunks WHERE doc_id=%s AND chunk_no=%s",
                        (d["doc_id"], i))
            row = cur.fetchone()
            if row and row[0] == sha:
                skipped += 1
                continue
            emb = model.encode(f"passage: {piece}", normalize_embeddings=True).tolist()
            cur.execute(
                """INSERT INTO kb_chunks (doc_id, source, title, url_path, chunk_no,
                                          content, content_sha, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (doc_id, chunk_no) DO UPDATE
                     SET content=EXCLUDED.content, content_sha=EXCLUDED.content_sha,
                         embedding=EXCLUDED.embedding, title=EXCLUDED.title,
                         url_path=EXCLUDED.url_path, ingested_at=now()""",
                (d["doc_id"], d["source"], d["title"], d["url_path"], i,
                 piece, sha, emb))
            total += 1
        # хвосты пропавших чанков дока — удалить (док сократился)
        cur.execute("DELETE FROM kb_chunks WHERE doc_id=%s AND chunk_no > %s",
                    (d["doc_id"], max(seen_ids) if seen_ids else -1))
    cur.execute("UPDATE kb_ingest_runs SET finished_at=now(), chunks=%s, "
                "note=%s WHERE id=%s",
                (total, f"embedded={total} unchanged={skipped}", run_id))
    conn.commit()
    cur.execute("SELECT count(*) FROM kb_chunks")
    print(f"done: embedded={total} unchanged={skipped} total_chunks={cur.fetchone()[0]}")


if __name__ == "__main__":
    main()
