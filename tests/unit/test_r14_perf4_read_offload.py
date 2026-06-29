"""PERF-4 (#186): request-path read handlers run a synchronous psycopg2 query,
so they must be plain `def` — FastAPI then dispatches them to the threadpool and
the sync DB read never blocks the event loop. (Connection pooling is pgbouncer's
job; no app-level pool. The 200k list cap stays a documented safety ceiling.)
"""
import inspect


def test_list_forecasts_is_sync_def():
    from src.api.routers import inference
    assert not inspect.iscoroutinefunction(inference.list_forecasts), \
        "list_forecasts must be `def` (#186 PERF-4) so its sync DB read threadpools"


def test_list_anomalies_is_sync_def():
    from src.api.routers import inference
    assert not inspect.iscoroutinefunction(inference.list_anomalies), \
        "list_anomalies must be `def` (#186 PERF-4) so its sync DB read threadpools"
