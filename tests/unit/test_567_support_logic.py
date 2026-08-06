"""#567: чистое ядро решений бота (support/api/logic.py).

Замер 2026-08-06: модель перефразирует маркер отказа; дубли чанков
одного дока вытесняют статьи из топ-3. Тесты грузят logic.py по пути —
тяжёлых зависимостей support/api в CI нет.
"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "support_logic", Path("support/api/logic.py"))
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


def test_refusal_literal_marker():
    assert logic.is_refusal("НЕТ_В_ДОКУМЕНТАЦИИ")
    assert logic.is_refusal("Ответ: НЕТ_В_ДОКУМЕНТАЦИИ.")


def test_refusal_paraphrases_from_measurement():
    # реальные ответы Qwen из замера 2026-08-06
    assert logic.is_refusal("Нет в документации.")
    assert logic.is_refusal("нет в документации")
    assert logic.is_refusal("К сожалению, этого нет в документации.")
    assert logic.is_refusal("Нет  в   документации")


def test_valid_answers_not_refusal():
    assert not logic.is_refusal("Start — до 1 500 SKU.")
    assert not logic.is_refusal("")
    assert not logic.is_refusal("В документации сказано: горизонт 28 дней.")


def test_dedupe_keeps_best_chunk_per_doc():
    hits = [
        {"title": "doc-a", "sim": 0.9},
        {"title": "doc-a", "sim": 0.85},
        {"title": "doc-b", "sim": 0.8},
        {"title": "doc-c", "sim": 0.79},
        {"title": "doc-d", "sim": 0.7},
    ]
    out = logic.dedupe_by_doc(hits)
    assert [h["title"] for h in out] == ["doc-a", "doc-b", "doc-c"]
    assert out[0]["sim"] == 0.9


def test_dedupe_fewer_than_k_docs():
    hits = [{"title": "doc-a", "sim": 0.9}, {"title": "doc-a", "sim": 0.8}]
    assert len(logic.dedupe_by_doc(hits)) == 1


def test_dedupe_resorts_merged_sources():
    hits = [{"title": "doc-b", "sim": 0.7}, {"title": "doc-a", "sim": 0.9}]
    assert logic.dedupe_by_doc(hits)[0]["title"] == "doc-a"
