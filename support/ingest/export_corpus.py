"""SUP-1 (#504): экспорт ЖИВОГО корпуса в снапшот-файл.

Запускается ТАМ, где доступен прод-API (внутри api-контейнера через
localhost, или с рабочей станции). supbot-контур намеренно НЕ имеет
сетевого доступа в прод (изоляция 152-ФЗ) — он читает снапшот, не API.

    docker exec -i docker-api-1 python3 - < export_corpus.py > corpus.json
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request

API = "http://localhost:8000"


def _get(path: str):
    return json.load(urllib.request.urlopen(f"{API}{path}", timeout=30))


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def main() -> None:
    docs = []
    for c in _get("/help/categories")["categories"]:
        cat = _get(f"/help/categories/{c['slug']}")
        for a in cat.get("articles", []):
            art = _get(f"/help/articles/{a['slug']}")
            docs.append({"doc_id": f"help:{art['slug']}", "source": "help",
                         "title": art["title"], "url_path": f"/help/a/{art['slug']}",
                         "text": _text(art.get("body_html") or art.get("body_md") or "")})
    news = _get("/news")
    for p in (news.get("posts") if isinstance(news, dict) else news) or []:
        post = _get(f"/news/{p['slug']}")
        docs.append({"doc_id": f"news:{post['slug']}", "source": "news",
                     "title": post["title"], "url_path": f"/news/{post['slug']}",
                     "text": _text(post.get("body_html") or post.get("body_md") or "")})
    plans = _get("/plans")["plans"]
    lines = ["Тарифы Sprosly и их лимиты:"]
    for p in plans:
        lines.append(
            f"- {p['display_name']} (модель {p['model_display_name']}): "
            f"до {p['max_skus'] or 'неограниченно'} SKU, горизонт "
            f"{p['max_horizon_days']} дней, датасетов {p.get('datasets_limit')}, "
            f"пауза {str(p['training_cooldown_hours']) + ' ч' if p['training_cooldown_hours'] else 'нет'}.")
    docs.append({"doc_id": "plans:limits", "source": "plans",
                 "title": "Тарифы и лимиты", "url_path": "/plans", "text": "\n".join(lines)})
    json.dump({"docs": docs}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
