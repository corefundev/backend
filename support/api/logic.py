"""Чистые решения бота — вынесены из app.py ради юнит-тестов (#567).

Замер 2026-08-06 (issue #567) показал два дефекта прод-пайплайна,
не зависящих от модели:
1. Модель может ПЕРЕФРАЗИРОВАТЬ служебный маркер отказа («Нет в
   документации.» вместо НЕТ_В_ДОКУМЕНТАЦИИ) — буквальная проверка
   принимала парафраз за валидный ответ и НЕ эскалировала.
2. В топ-3 контекста попадали несколько чанков одного документа —
   контекст терял разнообразие, правильные статьи вытеснялись.
"""
import re

NOANS = "НЕТ_В_ДОКУМЕНТАЦИИ"

_REFUSAL_RE = re.compile(r"нет\s+в\s+документации", re.IGNORECASE)


def is_refusal(text: str) -> bool:
    """Отказ модели: буквальный маркер ИЛИ его парафраз."""
    return NOANS in text or bool(_REFUSAL_RE.search(text))


def dedupe_by_doc(hits: list[dict], k: int = 3) -> list[dict]:
    """Не больше одного (лучшего по sim) чанка на документ, топ-k по sim.

    hits — уже отсортированы retrieve() по убыванию sim; сортируем ещё
    раз на случай объединённых источников.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for h in sorted(hits, key=lambda h: -h["sim"]):
        if h["title"] in seen:
            continue
        seen.add(h["title"])
        out.append(h)
        if len(out) == k:
            break
    return out
