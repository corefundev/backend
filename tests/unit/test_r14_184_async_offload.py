"""#184 (R14-5 / ASYNC-1): hot handlers with blocking sync bodies must NOT be
`async def` (which would run them on the event loop) — they are plain `def` so
FastAPI/Starlette dispatches them to the threadpool, keeping the loop free.

Structural guards: cheap, deterministic, no event-loop load test needed.
"""
import inspect


def test_predict_is_sync_def_for_threadpool_offload():
    # Body is blocking sync I/O (psycopg2 registry, S3 model load) + CPU-heavy
    # pandas/ML — must be `def` so it runs off the event loop.
    from src.api.routers import inference
    assert not inspect.iscoroutinefunction(inference.predict), \
        "predict must be `def` (#184) so FastAPI threadpools it"


def test_get_token_is_sync_def():
    # bcrypt cost-12 verify is ~250-400ms pure CPU — must not run on the loop.
    from src.api.routers import auth
    assert not inspect.iscoroutinefunction(auth.get_token), \
        "get_token must be `def` (#184) so bcrypt runs off the event loop"


def test_predict_batch_offloads_via_threadpool():
    # predict_batch stays async but must dispatch each predict through the
    # threadpool (real parallelism) — assert the source wires run_in_threadpool.
    import src.api.routers.inference as inf
    src = inspect.getsource(inf.predict_batch)
    assert "run_in_threadpool(predict" in src, \
        "predict_batch must gather predict() via run_in_threadpool (#184)"
