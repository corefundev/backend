"""MA-7/M2 (#525, аудит): walk-forward фолды меряют clean-holdout бленд —
тот же, что сервится финальной моделью (#152), а не legacy pseudo-holdout."""
from pathlib import Path


def test_fold_prefers_clean_blend_and_falls_back():
    wf = Path("src/validation/walk_forward.py").read_text()
    seg = wf.split('hasattr(fold_model, "compute_blend_weights")')[1]
    assert "compute_blend_weights_clean" in seg
    assert "blend_clean_holdout" in seg          # тот же конфиг-гейт, что train
    # legacy остаётся только фолбэком при неуспехе clean
    assert "if not _clean_ok" in seg


def test_same_gate_key_as_final_fit():
    tr = Path("src/pipeline/train.py").read_text()
    assert 'blend_clean_holdout' in tr           # источник семантики гейта
