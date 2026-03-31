from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Insight, InsightAnchor

STORAGE_DIR = ".glance"
STORAGE_FILE = "insights.json"


class InsightStore:
    """Persists insights to a JSON file in the project directory."""

    def __init__(self, project_root: str = "."):
        self.root = Path(project_root)
        self.storage_dir = self.root / STORAGE_DIR
        self.storage_file = self.storage_dir / STORAGE_FILE
        self._ensure_storage()

    def _ensure_storage(self):
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if not self.storage_file.exists():
            self._write_insights([])
        # Add .glance to .gitignore if it exists
        gitignore = self.root / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text()
            if ".glance" not in content:
                with open(gitignore, "a") as f:
                    f.write("\n# Glance memory\n.glance/\n")

    def _read_insights(self) -> list[Insight]:
        try:
            data = json.loads(self.storage_file.read_text())
            return [Insight(**s) for s in data]
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write_insights(self, insights: list[Insight]):
        data = [s.model_dump() for s in insights]
        self.storage_file.write_text(json.dumps(data, indent=2))

    def upsert(self, insight: Insight) -> tuple[Insight, bool]:
        """
        Insert or update an insight. If an insight with the same file+from_text
        exists, overwrite it (preserving the original ID).
        Returns (insight, was_update).
        """
        insights = self._read_insights()
        for i, existing in enumerate(insights):
            if existing.matches_region(insight.file, insight.anchor.from_text):
                # Upsert: keep ID, update everything else
                insight.id = existing.id
                insight.created_at = existing.created_at
                insight.updated_at = datetime.now(timezone.utc).isoformat()
                insight.stale_views = 0  # Reset on re-creation
                insights[i] = insight
                self._write_insights(insights)
                return insight, True

        # New insight
        insights.append(insight)
        self._write_insights(insights)
        return insight, False

    def get_by_tags(self, tags: list[str]) -> list[Insight]:
        """Get all insights matching ANY of the given tags."""
        insights = self._read_insights()
        if not tags:
            return insights
        tag_set = set(tags)
        return [s for s in insights if tag_set.intersection(s.tags)]

    def get_by_file(self, file: str) -> list[Insight]:
        """Get all insights for a specific file."""
        insights = self._read_insights()
        return [s for s in insights if s.file == file]

    def get_all(self) -> list[Insight]:
        return self._read_insights()

    def increment_stale_views(self, insight_id: str):
        """Increment the stale view counter for an insight."""
        insights = self._read_insights()
        for s in insights:
            if s.id == insight_id:
                s.stale_views += 1
                break
        self._write_insights(insights)

    def delete(self, insight_id: str) -> bool:
        """Delete an insight by ID."""
        insights = self._read_insights()
        original_len = len(insights)
        insights = [s for s in insights if s.id != insight_id]
        self._write_insights(insights)
        return len(insights) < original_len

    def delete_many(self, insight_ids: list[str]) -> int:
        """Delete multiple insights. Returns count deleted."""
        insights = self._read_insights()
        id_set = set(insight_ids)
        original_len = len(insights)
        insights = [s for s in insights if s.id not in id_set]
        self._write_insights(insights)
        return original_len - len(insights)

    def update_last_viewed(self, insight_ids: list[str]):
        """Set last_viewed to now for the given insight IDs."""
        now = datetime.now(timezone.utc).isoformat()
        insights = self._read_insights()
        id_set = set(insight_ids)
        for s in insights:
            if s.id in id_set:
                s.last_viewed = now
        self._write_insights(insights)

    def remove_tag(self, tag: str) -> tuple[int, int]:
        """Remove a tag from all insights. Deletes orphaned insights (no tags left).
        Returns (insights_modified, orphans_deleted)."""
        insights = self._read_insights()
        modified = 0
        orphans = 0
        surviving = []
        for s in insights:
            if tag in s.tags:
                s.tags = [t for t in s.tags if t != tag]
                modified += 1
                if not s.tags:
                    orphans += 1
                    continue  # skip adding to surviving
            surviving.append(s)
        self._write_insights(surviving)
        return modified, orphans

    def get_all_tags(self) -> dict[str, list[Insight]]:
        """Return a dict mapping each tag to its insights."""
        insights = self._read_insights()
        tags: dict[str, list[Insight]] = {}
        for s in insights:
            for t in s.tags:
                tags.setdefault(t, []).append(s)
        return tags
