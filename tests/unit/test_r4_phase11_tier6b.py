"""
Regression tests for Round-4 Phase 11 Tier 6b (2026-05-17).

R4-18 — SBOM coverage for ALL THREE shipped images (api + worker +
        sandbox) in release.yml, not just api. Each SBOM gets its
        own cosign sign-blob sidecar (.sig + .crt) so a tampered
        release asset can be detected per-image.
R4-19 — Trivy image scan extended from api-only to a matrix over
        [api, worker, sandbox]. fail-fast: false so a HIGH/CRITICAL
        in one image doesn't mask findings in the others. Per-image
        SARIF gets a distinct category in the GH Security tab.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


# ── R4-19: Trivy matrix in ci.yml ────────────────────────────────────────

def test_trivy_image_scan_uses_matrix_over_three_images():
    """ci.yml's image-scan job must use a matrix strategy with all
    three image targets. Single-image scanning silently missed CVEs
    in worker / sandbox layers."""
    text = (_BACKEND / ".github" / "workflows" / "ci.yml").read_text()
    scan_idx = text.find("image-scan:")
    assert scan_idx > 0
    # Find the end of the job block — next top-level job header.
    # Heuristic: next 2-space indented `<name>:` after `image-scan`.
    end_idx = text.find("\n  pip-audit:", scan_idx)
    if end_idx < 0:
        end_idx = len(text)
    block = text[scan_idx:end_idx]

    assert "strategy:" in block, (
        "image-scan must use strategy: for matrix (R4-19)"
    )
    assert "matrix:" in block, (
        "image-scan must declare matrix.image (R4-19)"
    )
    # fail-fast: false so all three scan even if one breaks.
    assert "fail-fast: false" in block, (
        "matrix must set fail-fast: false so api-fail doesn't hide worker/sandbox findings"
    )
    # All three image names present in the matrix.
    for name in ("api", "worker", "sandbox"):
        assert f"name: {name}" in block, (
            f"matrix.image must include {name} (R4-19)"
        )
    # Per-Dockerfile reference in the matrix entries.
    assert "docker/Dockerfile.worker" in block, (
        "matrix must reference Dockerfile.worker"
    )
    assert "docker/Dockerfile.sandbox" in block, (
        "matrix must reference Dockerfile.sandbox"
    )


def test_trivy_sarif_uploaded_per_image_with_category():
    """SARIF uploads must use a per-image `category` so findings don't
    collapse across api/worker/sandbox in the GH Security tab."""
    text = (_BACKEND / ".github" / "workflows" / "ci.yml").read_text()
    scan_idx = text.find("image-scan:")
    end_idx = text.find("\n  pip-audit:", scan_idx)
    block = text[scan_idx:end_idx] if end_idx > scan_idx else text[scan_idx:]
    # The sarif_file expression is per-matrix.
    assert "trivy-${{ matrix.image.name }}.sarif" in block, (
        "SARIF file must be per-image, not the hardcoded trivy-api.sarif"
    )
    assert "category: trivy-${{ matrix.image.name }}" in block, (
        "upload-sarif must set per-image category (R4-19)"
    )


def test_trivy_scan_keeps_hard_fail_gate():
    """The SARIF-emit Trivy invocation must still pass --exit-code 1
    so HIGH/CRITICAL findings remain a hard CI gate (not advisory)
    after the matrix refactor."""
    text = (_BACKEND / ".github" / "workflows" / "ci.yml").read_text()
    scan_idx = text.find("image-scan:")
    end_idx = text.find("\n  pip-audit:", scan_idx)
    block = text[scan_idx:end_idx] if end_idx > scan_idx else text[scan_idx:]
    assert "--exit-code 1" in block, (
        "Trivy SARIF pass must keep --exit-code 1 hard-gate (R4-19)"
    )
    assert "--severity HIGH,CRITICAL" in block, (
        "Trivy gate must scan HIGH+CRITICAL"
    )


# ── R4-18: SBOM for worker + sandbox in release.yml ──────────────────────

def test_release_generates_sbom_for_all_three_images():
    """release.yml must run anchore/sbom-action for api + worker +
    sandbox — not just api. Each SBOM points at the published GHCR
    image so it matches what consumers pull."""
    text = (_BACKEND / ".github" / "workflows" / "release.yml").read_text()
    for svc in ("api", "worker", "sandbox"):
        assert f"output-file: sbom-{svc}.spdx.json" in text, (
            f"release.yml must generate sbom-{svc}.spdx.json (R4-18)"
        )
        assert f"sku-{svc}:${{{{ steps.vars.outputs.tag }}}}" in text, (
            f"sbom-action must reference the GHCR sku-{svc} image"
        )


def test_release_signs_each_sbom_with_sign_blob():
    """Each SBOM must get its own cosign sign-blob .sig + .crt sidecar
    so tampering with any one SBOM is independently detectable."""
    text = (_BACKEND / ".github" / "workflows" / "release.yml").read_text()
    # The keyless sign-blob loop must exist.
    assert "cosign sign-blob" in text, (
        "release.yml must run cosign sign-blob for SBOMs (R3-23 / R4-18)"
    )
    # The for-loop iterates over all three services.
    sign_idx = text.find("Sign SBOMs (cosign sign-blob keyless)")
    assert sign_idx > 0, (
        "release.yml must have a step that signs all SBOMs (R4-18)"
    )
    end_idx = text.find("- name: Create GitHub Release", sign_idx)
    block = text[sign_idx:end_idx] if end_idx > sign_idx else text[sign_idx:]
    assert "for svc in api worker sandbox" in block, (
        "sign-blob loop must cover all three SBOM services"
    )


def test_release_attaches_all_nine_sbom_artefacts():
    """The release asset list must include the full 3×3 set: .json +
    .sig + .crt for api/worker/sandbox. Missing any one breaks
    auditability for that image."""
    text = (_BACKEND / ".github" / "workflows" / "release.yml").read_text()
    expected = []
    for svc in ("api", "worker", "sandbox"):
        expected.extend([
            f"sbom-{svc}.spdx.json",
            f"sbom-{svc}.spdx.json.sig",
            f"sbom-{svc}.spdx.json.crt",
        ])
    for f in expected:
        assert f in text, (
            f"release.yml `files:` list must attach {f} (R4-18)"
        )
