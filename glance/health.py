from __future__ import annotations

from difflib import SequenceMatcher

from .models import Insight


# Thresholds
HEALTHY_THRESHOLD = 0.8  # Above this = healthy
STALE_THRESHOLD = 0.4  # Below this = stale, flagged for deletion
MAX_STALE_VIEWS = 2  # After this many stale views without refresh, mark for deletion


def compute_health(original: str, current: str) -> float:
    """
    Compute health score between original and current content.

    Returns float 0.0-1.0:
      1.0 = identical
      >0.8 = healthy (minor edits)
      0.4-0.8 = degraded (notable changes)
      <0.4 = stale (major rewrite, flag for deletion)
    """
    # Fast path: identical
    if original == current:
        return 1.0

    # Fast path: one is empty
    if not original or not current:
        return 0.0

    # Normalize whitespace for comparison
    orig_normalized = _normalize(original)
    curr_normalized = _normalize(current)

    if orig_normalized == curr_normalized:
        return 0.99  # Whitespace-only changes

    # SequenceMatcher gives us a good similarity ratio
    ratio = SequenceMatcher(None, orig_normalized, curr_normalized).ratio()

    return round(ratio, 3)


def assess_insight(insight: Insight, current_content: str | None) -> InsightHealth:
    """Assess an insight's health given its current resolved content."""
    if current_content is None:
        return InsightHealth(
            score=0.0,
            status="broken",
            message=f"Could not resolve insight in {insight.file}",
        )

    current_hash = Insight.hash_content(current_content)

    # Identical
    if current_hash == insight.original_hash:
        return InsightHealth(score=1.0, status="healthy", message="Unchanged")

    score = compute_health(insight.original_content, current_content)

    if score >= HEALTHY_THRESHOLD:
        return InsightHealth(
            score=score,
            status="healthy",
            message="Minor changes detected",
        )
    elif score >= STALE_THRESHOLD:
        return InsightHealth(
            score=score,
            status="degraded",
            message="Notable changes detected since insight was created",
        )
    else:
        views_left = MAX_STALE_VIEWS - insight.stale_views
        if views_left <= 0:
            return InsightHealth(
                score=score,
                status="expired",
                message="Major changes detected. This insight will be deleted. Re-create it to keep it alive.",
            )
        return InsightHealth(
            score=score,
            status="stale",
            message=f"Major changes detected. Will be deleted after {views_left} more view(s) unless re-created.",
        )


class InsightHealth:
    def __init__(self, score: float, status: str, message: str):
        self.score = score
        self.status = status  # healthy | degraded | stale | expired | broken
        self.message = message

    def should_flag_deletion(self) -> bool:
        """Whether to warn the agent this insight is dying."""
        return self.status in ("stale", "expired", "broken")

    def should_delete(self) -> bool:
        """Whether to actually remove this insight."""
        return self.status in ("expired", "broken")


def _normalize(text: str) -> str:
    """Normalize whitespace for comparison."""
    lines = text.splitlines()
    stripped = [line.strip() for line in lines if line.strip()]
    return "\n".join(stripped)
