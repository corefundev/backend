"""
Legal documents API — public GET + admin PUT.

R5-M1 (2026-05-18): extracted from src/api/main.py as the first
slice of the god-module split. Chosen first for lowest blast
radius — two endpoints, no shared state with the rest of the
codebase beyond `get_current_client` (already in jwt_auth) and
`get_legal_store` (already in src.storage.legal).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth.jwt_auth import AuthContext, get_current_client
from src.storage.legal import get_legal_store


router = APIRouter(tags=["legal"])


class LegalDocUpdate(BaseModel):
    # R11-L17: validate + bound the admin-supplied body (was a raw `dict`
    # with unbounded `content` flowing into a TEXT column). 1 MB easily
    # covers a privacy policy / terms document.
    title:   str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1, max_length=1_000_000)


@router.get("/legal/{doc_id}")
async def get_legal_doc(doc_id: str):
    """
    Public endpoint — fetches a legal document by id. Used by /privacy
    page and the signup-flow checkbox link. No auth required.
    """
    doc = get_legal_store().get(doc_id)
    if not doc:
        raise HTTPException(404, f"unknown legal document: {doc_id}")
    return {
        "doc_id":     doc.doc_id,
        "title":      doc.title,
        "content":    doc.content,
        "version":    doc.version,
        "updated_at": doc.updated_at.isoformat(),
    }


@router.put("/admin/legal/{doc_id}")
async def update_legal_doc(
    doc_id: str,
    body: LegalDocUpdate,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Admin endpoint — overwrite the document's title + content. Bumps
    version automatically. Requires admin role.
    """
    auth.require_role("admin")
    title   = body.title.strip()
    content = body.content.strip()
    if not title or not content:
        raise HTTPException(400, "title and content are required")
    doc = get_legal_store().upsert(
        doc_id=doc_id,
        title=title,
        content=content,
        updated_by=auth.client_id,
    )
    return {
        "doc_id":     doc.doc_id,
        "title":      doc.title,
        "version":    doc.version,
        "updated_at": doc.updated_at.isoformat(),
    }
