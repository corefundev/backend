"""
Audit-trail API — recent security events for a client (logins,
OAuth, plan/password changes).

R5-M1 (2026-05-18) slice 2: extracted from src/api/main.py. Single
GET endpoint, narrow dependency surface (just `list_for_client`
from src.audit + `require_client_access` from jwt_auth).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.audit import list_for_client
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access


router = APIRouter(tags=["audit"])


@router.get("/clients/{client_id}/audit")
async def list_audit_events(
    client_id: str,
    limit:  int = 100,
    offset: int = 0,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Recent security events for the caller's account: logins, OAuth,
    plan changes, password changes. Backs the "Security" tab in the
    UI. Caller can only see their own events — admin role can read
    any client's via the same endpoint.

    Bounded params (limit ≤ 500) so a malicious caller can't DOS the
    DB with a huge OFFSET scan.
    """
    require_client_access(client_id, auth)
    if limit < 1 or limit > 500:
        raise HTTPException(422, detail="limit must be between 1 and 500")
    if offset < 0:
        raise HTTPException(422, detail="offset must be ≥ 0")
    events = list_for_client(client_id, limit=limit, offset=offset)
    return {
        "client_id": client_id,
        "count":     len(events),
        "events":    [
            {
                "id":            e.id,
                "ts":            e.ts,
                "event_type":    e.event_type,
                "event_subtype": e.event_subtype,
                "actor_email":   e.actor_email,
                "target_type":   e.target_type,
                "target_id":     e.target_id,
                "ip":            e.ip,
                "user_agent":    e.user_agent,
                "success":       e.success,
                "metadata":      e.metadata,
            }
            for e in events
        ],
    }
