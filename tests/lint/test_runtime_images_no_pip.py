"""Гард: pip удаляется из ВСЕХ рантайм-образов финальным слоем.

Контракт (2026-08-02): pip — build-инструмент; его вендорённые копии
(pip/_vendor: msgpack, setuptools, wheel, …) регулярно получают CVE и
валят Trivy-скан, хотя в реальном дереве зависимостей образов стоят
патченные версии. Решение — физическое удаление pip из финального слоя
(class-elimination), а не подавление сканера. Этот тест не даёт срезу
молча исчезнуть при будущих правках Dockerfile'ов.

По правилу static-shell-тестов ассертим ПОДСТРОКУ команды, а не
однострочный regex (line-continuation ломает regex-подход).
"""
from pathlib import Path

import pytest

_DOCKERFILES = (
    "docker/Dockerfile",
    "docker/Dockerfile.worker",
    "docker/Dockerfile.sandbox",
)

_STRIP_CMD = "python -m pip uninstall -y pip"


@pytest.mark.parametrize("dockerfile", _DOCKERFILES)
def test_runtime_image_strips_pip(dockerfile):
    text = Path(dockerfile).read_text()
    assert _STRIP_CMD in text, (
        f"{dockerfile}: нет финального среза pip ({_STRIP_CMD!r}) — "
        "build-инструмент вернулся в рантайм-образ")


@pytest.mark.parametrize("dockerfile", _DOCKERFILES)
def test_pip_strip_is_after_last_install(dockerfile):
    """Срез должен быть ПОСЛЕДНЕЙ pip-операцией: install после uninstall
    вернул бы pip (и его вендоры) в финальный слой."""
    text = Path(dockerfile).read_text()
    strip_pos = text.rindex(_STRIP_CMD)
    last_install = text.rindex("pip install")
    assert strip_pos > last_install, (
        f"{dockerfile}: 'pip install' встречается ПОСЛЕ среза pip — "
        "финальный слой снова содержит pip")
