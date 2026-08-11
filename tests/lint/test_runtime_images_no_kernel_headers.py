"""Гард (Trivy 2026-08-11): linux-libc-dev выпилен из рантайм-образов.

Заголовки ядра приезжают каскадом за gcc/g++ (сборка колёс) и регулярно
ловят kernel-CVE, патчи которых отстают в debian-зеркалах — гейт красный
без нашей вины. Решение — purge из итоговой ФС после pip install
(в рантайме ничего не компилируется) с явным закреплением рантайм-
библиотек (libgomp1 — lightgbm, libpq5 — страховка).

По правилу static-shell-тестов ассертим ПОДСТРОКИ команд.
"""
from pathlib import Path

import pytest

_DOCKERFILES = ("docker/Dockerfile", "docker/Dockerfile.worker")

_PURGE = "apt-get purge -y linux-libc-dev"
_PIN = "apt-mark manual libgomp1 libpq5"


@pytest.mark.parametrize("dockerfile", _DOCKERFILES)
def test_kernel_headers_purged(dockerfile):
    text = Path(dockerfile).read_text()
    assert _PURGE in text, (
        f"{dockerfile}: purge linux-libc-dev исчез — kernel-CVE класс "
        "снова валит Trivy-гейт")
    assert _PIN in text, (
        f"{dockerfile}: закрепление рантайм-библиотек исчезло — autoremove "
        "может унести libgomp1 (lightgbm упадёт в рантайме)")


@pytest.mark.parametrize("dockerfile", _DOCKERFILES)
def test_purge_after_pip_installs(dockerfile):
    """Purge обязан стоять ПОСЛЕ последнего pip install: тулчейн ещё
    нужен для сборки колёс."""
    text = Path(dockerfile).read_text()
    assert text.rindex("pip install") < text.index(_PURGE)
