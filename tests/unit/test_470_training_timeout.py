"""#470: потолок RQ-таймаута обучения ≥ 3ч.

Live-инцидент 2026-08-13: легитимный Business-прогон (~104 мин
наблюдаемо) выбился за прежний потолок 7200с из-за вариативности
внешних источников — RQ убил джоб на середине, клиент остался без
модели. Потолок обязан нести запас к наблюдаемой длительности.
"""
import inspect

from src.pipeline.task_queue import enqueue_training


def test_training_timeout_has_headroom():
    default = inspect.signature(enqueue_training).parameters["timeout"].default
    assert default >= 10800, (
        f"training timeout {default}s < 3h — наблюдаемая длительность "
        "Business-прогона ~104 мин, потолок впритык убивает легитимные "
        "обучения (инцидент 2026-08-13)")
