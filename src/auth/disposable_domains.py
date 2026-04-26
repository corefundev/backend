"""
src/auth/disposable_domains.py

Block known throwaway email providers at signup.

Trade-off this module makes
───────────────────────────
The list catches casual abuse — Mailinator, Guerrilla Mail, etc. —
where the registrant grabs whatever throwaway address loads first.
A motivated attacker can register a custom domain ($5/year) and route
it to a temp inbox; we'd never catch that. So the goal is to raise
the per-account cost from "free in 10 seconds" to "needs a domain
purchase or non-trivial setup". That's enough to deter most abuse.

False-positive risk
───────────────────
Some services blur the line — e.g. ProtonMail and Tutanota are
privacy-respecting personal mail providers, not disposable. They are
deliberately NOT in the bundled list. Periodically audit the file
to make sure no legitimate provider sneaks in via mass-update from
the upstream community list.

Performance
───────────
Loaded once at process start, kept as a frozenset in memory.
Lookup is O(1). The file holds ~50 entries; even the full 12 000-row
upstream list fits in <1 MB.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = "configs/disposable_domains.txt"

_cache: Optional[FrozenSet[str]] = None


def _load_from(path: Path) -> FrozenSet[str]:
    if not path.exists():
        logger.warning(
            "disposable_domains: file not found at %s — block list is empty",
            path,
        )
        return frozenset()

    domains: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Be defensive: lowercase, strip trailing dots, no spaces.
            d = line.lower().rstrip(".")
            if " " in d or "@" in d:
                logger.warning("disposable_domains: skipping malformed line %r", line)
                continue
            domains.add(d)
    logger.info("disposable_domains: loaded %d entries from %s", len(domains), path)
    return frozenset(domains)


def get_block_list() -> FrozenSet[str]:
    """
    Returns the cached set. First call lazily loads from
    DISPOSABLE_DOMAINS_FILE env or the default bundled file.
    """
    global _cache
    if _cache is None:
        path = Path(os.environ.get("DISPOSABLE_DOMAINS_FILE", DEFAULT_PATH))
        _cache = _load_from(path)
    return _cache


def is_disposable(email: str) -> bool:
    """
    True if the email's domain matches the block list.

    Matches both the literal domain and any parent (so
    `foo.mailinator.com` is caught when `mailinator.com` is in the
    list). This handles services that hand out random subdomains.
    """
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower().rstrip(".")
    block = get_block_list()
    if domain in block:
        return True
    # Walk parent domains: foo.bar.mailinator.com → bar.mailinator.com
    # → mailinator.com.
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in block:
            return True
    return False


def reset_cache_for_tests() -> None:
    global _cache
    _cache = None
