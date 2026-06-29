"""SEC-1 + BUG-2 (#186): symmetric config validation on the register_client write
path, and graceful (422-not-500) handling of non-numeric weather coordinates.
"""
import inspect

from src.clients.config_manager import validate_client_config


# ── BUG-2: non-numeric lat/lon → clean error (422), not an unguarded float() 500
def test_bug2_non_numeric_latlon_returns_error_not_exception():
    errs = validate_client_config({"features": {"weather": {"latitude": "abc"}}})
    assert any("latitude" in e and "must be a number" in e for e in errs)

    errs = validate_client_config({"features": {"weather": {"longitude": [1, 2]}}})
    assert any("longitude" in e for e in errs)

    # out-of-range numeric still rejected (with the same message as before)
    assert validate_client_config({"features": {"weather": {"latitude": 200}}})
    assert validate_client_config({"features": {"weather": {"longitude": -999}}})

    # valid coordinates pass
    assert validate_client_config(
        {"features": {"weather": {"latitude": 55.75, "longitude": 37.62}}}
    ) == []


# ── SEC-1: the R13-1 forbidden-server-managed-key guard the route now runs
def test_sec1_validate_rejects_r13_1_forbidden_keys():
    assert validate_client_config({"features": {"weather": {"cache_path": "/etc/x"}}})
    assert validate_client_config({"anything_dir": "/tmp/evil"})
    assert validate_client_config({"mlflow": {"tracking_uri": "http://evil"}})
    assert validate_client_config({"api": {"rate_limit": 0}})


def test_sec1_register_client_wires_the_validation():
    # Guards against the exact regression the audit found: register_client storing
    # req.config verbatim with NO validation. Its body must run the same guards as
    # the PUT/PATCH config path.
    from src.api.routers import clients
    src = inspect.getsource(clients.register_client)
    assert "validate_client_config" in src, "register_client must validate config (SEC-1)"
    assert "assert_config_keys_allowed" in src, "register_client must enforce plan-tier keys"
    assert "assert_horizon_within_plan" in src, "register_client must clamp horizon to plan"
