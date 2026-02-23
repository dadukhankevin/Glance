from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class ShardAnchor(BaseModel):
    """How a shard is anchored in a file."""

    from_text: str
    to_text: str
    # If we detected a containing function, store it for fallback resolution
    function_anchor: Optional[str] = None
    # Resolved line range (absolute, for fast-path)
    start_line: Optional[int] = None
    end_line: Optional[int] = None


class Shard(BaseModel):
    """A single memory shard — a live window into a code region."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    file: str
    anchor: ShardAnchor
    original_content: str
    original_hash: str
    summary: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_viewed: Optional[str] = None
    # How many times this shard has been viewed while unhealthy
    stale_views: int = 0

    @staticmethod
    def hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def matches_region(self, file: str, from_text: str) -> bool:
        """Check if this shard points at the same region (for upsert logic)."""
        return self.file == file and self.anchor.from_text == from_text
