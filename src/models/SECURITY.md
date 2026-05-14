# src/models — pickle security boundary (audit R2-16)

Every `Forecaster.load(path)` / `Clusterer.load(path)` classmethod in this
package does a **raw `pickle.load(f)`** on a local file. That's a hard RCE
surface if the file path can be controlled by an attacker.

Why those classmethods are kept anyway: tests + offline notebooks open
pickle files from already-verified scratch dirs where the writer is
trusted. We need a way to round-trip a model state without dragging in
the full S3 storage layer.

**Production code MUST NOT call these classmethods directly.** The single
sanctioned model-load path is:

    src.pipeline.inference_utils.load_model_any_format(path, config)

which delegates to `src.storage.backend._verify(blob)` (HMAC envelope
check) before passing bytes to `pickle.loads`. Any unsigned pickle is
rejected with `ValueError("signed pickle found but MODEL_SIGNING_KEY
not configured")` or the equivalent.

Audit decision: leave `.load(path)` classmethods in place (tests rely on
them), but DO NOT wire them into any new production code path. Verified
in grep across `src/` 2026-05-15: zero production callers outside this
package. The single internal call (`online_learning.py:191`) is itself
inside an orphaned module that no API endpoint reaches.

If a new path requires loading a `Forecaster` from disk in production,
add an HMAC-signed wrapper in `src/storage/backend.py` and route through
that — do NOT extend the raw `.load(path)` API.
