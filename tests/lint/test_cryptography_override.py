"""Гард #605: security-override cryptography==50.0.0 в api/worker образах.

Контракт (2026-08-06): CVE-2026-69247 закрыт только в cryptography
50.0.0, а mlflow 3.15.1 держит потолок <50 — резолвер не пропустит 50.x
через requirements.txt. Поэтому образы ставят cryptography 50.0.0
осознанным override'ом (--no-deps) ПОСЛЕ requirements и ДО среза pip,
с build-time пробой импортов. Этот тест не даёт override'у молча
исчезнуть при будущих правках Dockerfile'ов или requirements.

Свернуть: когда mlflow отпустит потолок до <51 — прямой пин 50.x в
requirements.txt, override и этот тест удалить В ОДНОМ PR.

По правилу static-shell-тестов ассертим ПОДСТРОКУ команды, а не
однострочный regex (line-continuation ломает regex-подход).
"""
from pathlib import Path

import pytest

_DOCKERFILES = (
    "docker/Dockerfile",
    "docker/Dockerfile.worker",
)

_OVERRIDE = 'pip install --no-cache-dir --no-deps "cryptography==50.0.0"'
_IMPORT_PROBE = "import cryptography, jwt, mlflow"
_PIP_STRIP = "python -m pip uninstall -y pip"


@pytest.mark.parametrize("dockerfile", _DOCKERFILES)
def test_override_present_with_probe(dockerfile):
    text = Path(dockerfile).read_text()
    assert _OVERRIDE in text, (
        f"{dockerfile}: security-override cryptography==50.0.0 исчез — "
        "CVE-2026-69247 снова открыт (см. #605)")
    assert _IMPORT_PROBE in text, (
        f"{dockerfile}: у override'а нет пробы импортов — билд не "
        "поймает поломку потребителей cryptography")


@pytest.mark.parametrize("dockerfile", _DOCKERFILES)
def test_override_before_pip_strip(dockerfile):
    """Override обязан стоять ДО среза pip: после среза ставить нечем."""
    text = Path(dockerfile).read_text()
    assert text.rindex(_OVERRIDE) < text.rindex(_PIP_STRIP), (
        f"{dockerfile}: override стоит после среза pip — слой не соберётся "
        "или откатит срез")


def test_requirements_documents_the_split():
    """requirements.txt обязан объяснять, почему пин 49 при override 50 —
    иначе будущий бамп «почистит» одно из двух."""
    text = Path("requirements.txt").read_text()
    assert "cryptography==49.0.0" in text
    assert "CVE-2026-69247" in text, (
        "requirements.txt: комментарий о двухступенчатом фиксе #605 удалён")
