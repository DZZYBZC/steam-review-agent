"""
Chunk lookup — serves cited patch-note chunk text by id for the evidence panel.

Reads from the existing ChromaDB collection (patches_{app_id}); no embedding
or retrieval is run here — pure metadata lookup.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from pipeline.retrieve import _get_client, _get_or_create_collection

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/chunks/{app_id}/{chunk_id}")
def get_chunk(app_id: str, chunk_id: str) -> dict[str, Any]:
    """Return the text and metadata for a single chunk id."""
    client = _get_client()
    collection = _get_or_create_collection(client, app_id)
    try:
        result = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
    except Exception as e:
        logger.exception("chunk lookup failed")
        raise HTTPException(status_code=500, detail=str(e))

    if not result.get("ids"):
        raise HTTPException(status_code=404, detail=f"chunk_id {chunk_id!r} not found in patches_{app_id}")

    doc = (result.get("documents") or [""])[0]
    meta = (result.get("metadatas") or [{}])[0] or {}
    return {
        "chunk_id": chunk_id,
        "app_id": app_id,
        "text": doc,
        "section_title": meta.get("section_title") or meta.get("title") or "",
        "parent_id": meta.get("parent_id") or "",
        "parent_context": meta.get("parent_context") or "",
        "bullet_index": meta.get("bullet_index"),
        "patch_date": meta.get("date") or meta.get("patch_date") or "",
    }
