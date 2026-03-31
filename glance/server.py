from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .health import assess_insight
from .models import Insight, InsightAnchor
from .resolver import detect_function_name, resolve_region
from .storage import InsightStore

# Project root defaults to cwd, can be overridden via env
PROJECT_ROOT = os.environ.get("GLANCE_PROJECT_ROOT", ".")

mcp = FastMCP(
    "glance",
    instructions="""Glance is a memory system. You save "insights" — pointers to code regions — and they stay live across sessions.

## Rules

1. Start every session by reading glance://tags. View insights before using Read/Grep/Glob.
2. After you explore code, create insights for what you found. Do not skip this. Do not wait to be asked.
3. Every Read/Grep/Glob that reveals something important must be followed by a create_insight call.

## Tools

- create_insight(file, from_text, to_text, tags) — save a code region
- view_insights(tags?, file?) — load insights by tag or file
- search_tags(query) — find tags by name
- delete_tag(tag) — remove a tag and clean up orphaned insights
- glance://tags — resource listing all tags. Check this first.

When you need information on a codebase search for insights BEFORE anything else (reading files, etc...).
If no insights exist, explore the codebase *then* add EVERYTHING relevent you've found as insights.
After every exploration/file read, add new insights.
EMPHASIS: ADD NEW INSIGHTS PROACTIVELY. It is helpful and good to add new insights.
Please, for the love of everything good, add NEW INSIGHTS PROACTIVELY!

You CANNOT defer the creation of insights, you must do it INSTANTLY after exploring, or reading, or searching. If you do not create insights right away, then you waste time.
""",
)

store = InsightStore(PROJECT_ROOT)


@mcp.tool()
def create_insight(
    file: str,
    from_text: str,
    to_text: str,
    tags: list[str],
) -> str:
    """Save a live window into a code region as an insight.

    The insight points to the region between from_text and to_text in the file.
    On future views, the live content at that location is shown, and health
    is tracked to detect when code has changed.

    If an insight already exists for the same file+from_text, it is overwritten
    (upsert behavior), which also resets its health. Use this to refresh
    stale insights.

    Args:
        file: Path to the source file (relative to project root)
        from_text: Text marking the start of the region (e.g. "def process_upload(")
        to_text: Text marking the end of the region (e.g. "return response")
        tags: Tags for organizing and querying insights (e.g. ["auth", "api"])
    """
    # Resolve the file path
    file_path = _resolve_file_path(file)
    if not Path(file_path).exists():
        return json.dumps({"error": f"File not found: {file}"})

    # Resolve the region
    region = resolve_region(file_path, from_text, to_text)
    if region is None:
        return json.dumps({
            "error": f"Could not find region in {file}. "
            f"Make sure from_text ('{from_text[:50]}...') appears in the file."
        })

    # Create the insight
    anchor = InsightAnchor(
        from_text=from_text,
        to_text=to_text,
        function_anchor=region.function_anchor,
        start_line=region.start_line,
        end_line=region.end_line,
    )

    insight = Insight(
        file=file,
        anchor=anchor,
        original_content=region.content,
        original_hash=Insight.hash_content(region.content),
        tags=tags,
    )

    insight, was_update = store.upsert(insight)

    action = "Updated" if was_update else "Created"
    result = {
        "status": "ok",
        "action": action.lower(),
        "insight_id": insight.id,
        "file": file,
        "lines": f"{region.start_line}-{region.end_line}",
        "tags": tags,
    }
    if region.function_anchor:
        result["function_anchor"] = region.function_anchor

    return json.dumps(result)


DEFAULT_LIMIT = 50


@mcp.tool()
def view_insights(
    tags: Optional[list[str]] = None,
    file: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> str:
    """View insights with live content and health status.

    You MUST provide at least one filter (tags or file). Use the glance://tags
    resource to discover available tags, then drill into specific ones.

    Insights always show the live code content from the file. Health tracking
    detects when code has changed since the insight was created.

    After viewing, insights that are stale or broken will be flagged. You can either:
    - Re-create them with create_insight to refresh (resets health)
    - Ignore them and they'll be deleted after a few more views

    Insights are returned oldest-first (so the most recent context is closest to
    your next response). By default, at most 50 insights are returned. Use offset
    to page through older insights.

    Args:
        tags: Filter insights by tags (returns insights matching ANY tag).
        file: Filter insights by file path. Can be combined with tags.
        limit: Max insights to return (default 50). Use with offset to page through results.
        offset: Skip this many insights (oldest first) before returning. Default 0.
    """
    # Require at least one filter
    if not tags and not file:
        return json.dumps({
            "error": "Provide at least one filter (tags or file). "
            "Use search_tags() or the glance://tags resource to discover available tags."
        })

    # Gather matching insights
    if tags:
        insights = store.get_by_tags(tags)
        if file:
            file_path = _resolve_file_path(file)
            insights = [s for s in insights if s.file == file or _resolve_file_path(s.file) == file_path]
    else:
        file_path = _resolve_file_path(file)
        insights = store.get_by_file(file) or store.get_by_file(file_path)

    if not insights:
        filter_desc = []
        if tags:
            filter_desc.append(f"tags={tags}")
        if file:
            filter_desc.append(f"file={file}")
        filter_str = ", ".join(filter_desc) if filter_desc else "any"
        return json.dumps({
            "status": "empty",
            "message": f"No insights found matching {filter_str}",
            "⚠ IMPORTANT": "If you explore this area manually, you MUST call create_insight() "
            "to save what you learn. Without insights, this knowledge is lost when the session ends."
        })

    # Sort oldest-first so most recent context is closest to the LLM's next response
    insights.sort(key=lambda s: s.created_at)

    # Paginate
    effective_limit = limit if limit is not None else DEFAULT_LIMIT
    total_matching = len(insights)
    insights = insights[offset:offset + effective_limit]

    results = []
    flagged_for_deletion = []
    to_delete_now = []

    for insight in insights:
        # Resolve live content
        file_path = _resolve_file_path(insight.file)
        region = resolve_region(
            file_path,
            insight.anchor.from_text,
            insight.anchor.to_text,
            function_anchor=insight.anchor.function_anchor,
        )

        current_content = region.content if region else None
        health = assess_insight(insight, current_content)

        # Build the view
        entry: dict = {
            "insight_id": insight.id,
            "file": insight.file,
            "tags": insight.tags,
            "health": {"score": health.score, "status": health.status, "message": health.message},
        }

        if region:
            entry["lines"] = f"{region.start_line}-{region.end_line}"

        # Show live content
        entry["content"] = current_content or "[Could not resolve]"

        # Track stale insights
        if health.should_delete():
            to_delete_now.append(insight.id)
            entry["⚠ action_required"] = "This insight has expired and will be deleted."
        elif health.should_flag_deletion():
            flagged_for_deletion.append(insight.id)
            store.increment_stale_views(insight.id)

        results.append(entry)

    # Track last_viewed for all viewed insights (excluding ones about to be deleted)
    viewed_ids = [s.id for s in insights if s.id not in set(to_delete_now)]
    if viewed_ids:
        store.update_last_viewed(viewed_ids)

    # Delete expired insights
    if to_delete_now:
        store.delete_many(to_delete_now)

    # Build response
    response: dict = {"insights": results, "count": len(results), "total": total_matching}
    if total_matching > offset + effective_limit:
        remaining = total_matching - (offset + effective_limit)
        next_offset = offset + effective_limit
        response["more"] = (
            f"{remaining} older insight(s) not shown. "
            f"Use view_insights(..., offset={next_offset}) to see more."
        )

    if flagged_for_deletion:
        response["⚠ attention"] = (
            f"Insights {flagged_for_deletion} have low confidence and will be "
            f"deleted soon unless you re-create them with create_insight."
        )

    if to_delete_now:
        response["🗑 deleted"] = (
            f"Insights {to_delete_now} were expired and have been deleted. "
            f"Re-explore these areas and create new insights if still needed."
        )

    return json.dumps(response, indent=2)


@mcp.tool()
def search_tags(query: str) -> str:
    """Search for tags by name (fuzzy substring match).

    Returns up to 5 matching tags with insight counts, sorted by relevance.
    Use this to discover what tags exist before querying insights.

    Args:
        query: Search string to match against tag names.
    """
    query_lower = query.lower()
    tag_map = store.get_all_tags()

    scored = []
    for tag, insights in tag_map.items():
        tag_lower = tag.lower()
        if query_lower in tag_lower:
            # Exact match scores highest, then prefix, then substring
            if tag_lower == query_lower:
                score = 0
            elif tag_lower.startswith(query_lower):
                score = 1
            else:
                score = 2
            scored.append((score, tag, len(insights)))

    scored.sort(key=lambda x: (x[0], -x[2]))
    results = [{"tag": tag, "insight_count": count} for _, tag, count in scored[:5]]

    if not results:
        return json.dumps({
            "results": [],
            "⚠ IMPORTANT": "No insights matched this search. If you explore code manually "
            "after this (using Read, Grep, Glob, etc.), you MUST call create_insight() to "
            "save what you learn. Without insights, this knowledge is lost when the session ends. "
            "Every important function, pattern, or decision you discover should become an insight."
        })

    return json.dumps(results)


@mcp.tool()
def delete_tag(tag: str) -> str:
    """Remove a tag from all insights. Insights left with no tags are deleted.

    Args:
        tag: The tag to remove.
    """
    modified, orphans = store.remove_tag(tag)
    if modified == 0:
        return json.dumps({"status": "not_found", "message": f"No insights have tag '{tag}'"})
    return json.dumps({
        "status": "ok",
        "tag": tag,
        "insights_modified": modified,
        "orphans_deleted": orphans,
    })


@mcp.resource("glance://tags")
def tags_resource() -> str:
    """Top tags ranked by most recent view activity."""
    tag_map = store.get_all_tags()

    def _rank_key(insights: list) -> str:
        """Return the most recent last_viewed (or updated_at) across insights."""
        timestamps = []
        for s in insights:
            timestamps.append(s.last_viewed or s.updated_at)
        return max(timestamps) if timestamps else ""

    if not tag_map:
        return json.dumps({
            "status": "empty",
            "message": "No insights exist yet for this codebase. "
            "As you explore code, use create_insight() to bookmark important regions "
            "(key functions, tricky logic, architectural decisions). "
            "Future sessions will have instant context instead of re-reading files."
        })

    ranked = sorted(tag_map.items(), key=lambda item: _rank_key(item[1]), reverse=True)
    results = [{"tag": tag, "insight_count": len(insights)} for tag, insights in ranked[:20]]
    return json.dumps(results)


def _resolve_file_path(file: str) -> str:
    """Resolve a file path relative to the project root."""
    path = Path(file)
    if path.is_absolute():
        return str(path)
    return str(Path(PROJECT_ROOT).resolve() / file)
