from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Shard, ShardAnchor

STORAGE_DIR = ".glance"
STORAGE_FILE = "shards.json"


class ShardStore:
    """Persists shards to a JSON file in the project directory."""

    def __init__(self, project_root: str = "."):
        self.root = Path(project_root)
        self.storage_dir = self.root / STORAGE_DIR
        self.storage_file = self.storage_dir / STORAGE_FILE
        self._ensure_storage()

    def _ensure_storage(self):
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if not self.storage_file.exists():
            self._write_shards([])
        # Add .glance to .gitignore if it exists
        gitignore = self.root / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text()
            if ".glance" not in content:
                with open(gitignore, "a") as f:
                    f.write("\n# Glance memory shards\n.glance/\n")

    def _read_shards(self) -> list[Shard]:
        try:
            data = json.loads(self.storage_file.read_text())
            return [Shard(**s) for s in data]
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write_shards(self, shards: list[Shard]):
        data = [s.model_dump() for s in shards]
        self.storage_file.write_text(json.dumps(data, indent=2))

    def upsert(self, shard: Shard) -> tuple[Shard, bool]:
        """
        Insert or update a shard. If a shard with the same file+from_text
        exists, overwrite it (preserving the original ID).
        Returns (shard, was_update).
        """
        shards = self._read_shards()
        for i, existing in enumerate(shards):
            if existing.matches_region(shard.file, shard.anchor.from_text):
                # Upsert: keep ID, update everything else
                shard.id = existing.id
                shard.created_at = existing.created_at
                shard.updated_at = datetime.now(timezone.utc).isoformat()
                shard.stale_views = 0  # Reset on re-creation
                shards[i] = shard
                self._write_shards(shards)
                return shard, True

        # New shard
        shards.append(shard)
        self._write_shards(shards)
        return shard, False

    def get_by_tags(self, tags: list[str]) -> list[Shard]:
        """Get all shards matching ANY of the given tags."""
        shards = self._read_shards()
        if not tags:
            return shards
        tag_set = set(tags)
        return [s for s in shards if tag_set.intersection(s.tags)]

    def get_by_file(self, file: str) -> list[Shard]:
        """Get all shards for a specific file."""
        shards = self._read_shards()
        return [s for s in shards if s.file == file]

    def get_all(self) -> list[Shard]:
        return self._read_shards()

    def increment_stale_views(self, shard_id: str):
        """Increment the stale view counter for a shard."""
        shards = self._read_shards()
        for s in shards:
            if s.id == shard_id:
                s.stale_views += 1
                break
        self._write_shards(shards)

    def delete(self, shard_id: str) -> bool:
        """Delete a shard by ID."""
        shards = self._read_shards()
        original_len = len(shards)
        shards = [s for s in shards if s.id != shard_id]
        self._write_shards(shards)
        return len(shards) < original_len

    def delete_many(self, shard_ids: list[str]) -> int:
        """Delete multiple shards. Returns count deleted."""
        shards = self._read_shards()
        id_set = set(shard_ids)
        original_len = len(shards)
        shards = [s for s in shards if s.id not in id_set]
        self._write_shards(shards)
        return original_len - len(shards)
