"""
Regression tests for R3-15 / R5-15 — nginx upstream DNS resolver fix
(2026-05-17).

Before this fix nginx resolved api:8000 once at worker startup and
cached the IP forever. Any api container recreate (CD deploy,
autoheal restart, manual `compose up`) produced 502s until
`nginx -s reload` ran — and the reload itself could race the new
container's network attach (observed during R4-21 deploy).

Fix: add Docker's embedded DNS resolver at 127.0.0.11 with a
10-second TTL, and switch every `proxy_pass` to the variable form
(`set $api_upstream "api:8000"; proxy_pass http://$api_upstream;`)
so nginx consults the resolver on each request instead of caching
at worker startup.

Trade-off: variable proxy_pass disables the keepalive pool to the
backend. On docker-bridge local connections, TCP setup is sub-ms —
acceptable for automatic IP-change recovery.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_NGINX_CONF = _BACKEND / "docker" / "nginx" / "nginx.conf"
_API_CONF   = _BACKEND / "docker" / "nginx" / "api.conf.template"


def test_nginx_conf_declares_docker_dns_resolver():
    """nginx.conf must declare `resolver 127.0.0.11 valid=10s ipv6=off`
    in the http{} block — otherwise the variable proxy_pass below
    can't resolve and every request 502s."""
    text = _NGINX_CONF.read_text()
    assert re.search(
        r"^\s*resolver\s+127\.0\.0\.11\s+valid=10s\s+ipv6=off\s*;",
        text, re.M,
    ), (
        "nginx.conf must declare `resolver 127.0.0.11 valid=10s ipv6=off;` "
        "in http{} (R3-15 / R5-15 — Docker embedded DNS)"
    )
    # resolver_timeout is a sensible companion.
    assert "resolver_timeout" in text, (
        "nginx.conf should also set resolver_timeout to bound DNS hangs"
    )


def test_nginx_conf_drops_static_upstream_block():
    """The old `upstream api_upstream { server api:8000; ... }` block
    must be removed (or commented). Its presence with variable
    proxy_pass would be misleading dead config."""
    text = _NGINX_CONF.read_text()
    # `upstream api_upstream {` as an actual block heading should be gone.
    assert not re.search(
        r"^\s*upstream\s+api_upstream\s*\{",
        text, re.M,
    ), (
        "static upstream api_upstream block must be removed — replaced "
        "by per-location `set $api_upstream` (R3-15 / R5-15)"
    )


def test_api_conf_uses_variable_proxy_pass():
    """Every `proxy_pass` in api.conf.template must use the variable
    form `http://$api_upstream` so nginx consults the resolver at
    request time, NOT the cached-at-startup `http://api_upstream`
    or hardcoded `http://api:8000`."""
    text = _API_CONF.read_text()
    # Exclude comment lines — docstrings legitimately reference the
    # old patterns as historical context.
    code_lines = "\n".join(
        l for l in text.splitlines()
        if not l.strip().startswith("#")
    )
    # Bad: any literal `proxy_pass http://api_upstream` or
    # `proxy_pass http://api:8000` (without variable) in CODE.
    assert "proxy_pass http://api_upstream" not in code_lines, (
        "api.conf.template must not use the legacy static-upstream form"
    )
    assert "proxy_pass http://api:8000" not in code_lines, (
        "api.conf.template must not hardcode the api hostname directly"
    )
    # Good: every proxy_pass references $api_upstream variable.
    proxy_pass_lines = [
        line for line in text.splitlines()
        if "proxy_pass" in line and not line.strip().startswith("#")
    ]
    # Filter out proxy_pass_header (different directive).
    proxy_pass_lines = [l for l in proxy_pass_lines if "proxy_pass_header" not in l]
    assert proxy_pass_lines, "expected at least one proxy_pass directive"
    for line in proxy_pass_lines:
        assert "$api_upstream" in line, (
            f"proxy_pass must use $api_upstream variable form: {line!r}"
        )


def test_api_conf_sets_variable_before_each_proxy_pass():
    """Each location block that does `proxy_pass http://$api_upstream`
    must precede it with `set $api_upstream "api:8000";` — nginx
    `set` is location-scoped, not inheritable from server{}."""
    text = _API_CONF.read_text()
    # Count occurrences of each pattern.
    set_count = len(re.findall(r'set\s+\$api_upstream\s+"api:8000"\s*;', text))
    pass_count = len([
        l for l in text.splitlines()
        if "proxy_pass http://$api_upstream" in l
    ])
    assert set_count == pass_count, (
        f"every proxy_pass needs its own `set $api_upstream`; "
        f"got {set_count} set, {pass_count} proxy_pass"
    )
    assert pass_count >= 4, (
        f"expected ≥4 proxy_pass blocks (metrics, auth/token, uploads, /), got {pass_count}"
    )
