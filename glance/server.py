from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .health import assess_shard
from .models import Shard, ShardAnchor
from .resolver import detect_function_name, resolve_region
from .storage import ShardStore

# Project root defaults to cwd, can be overridden via env
PROJECT_ROOT = os.environ.get("GLANCE_PROJECT_ROOT", ".")

mcp = FastMCP(
    "glance",
    instructions="""Glance is a memory system that saves live windows into code regions.
Shards are pointers (file + from_text + to_text) that resolve to current content on every view.

## IMPORTANT — When to use Glance

**Start of every session:** Check the glance://tags resource to see what memory
exists. Then call view_shards(tags=["relevant-tag"]) for tags related to your
current task. This gives you instant context — skip redundant file reads.

**Before exploring code with other tools:** ALWAYS check Glance first. Before
using Read, Grep, Glob, or any file exploration tool, check if relevant shards
already exist. Search tags related to your task — if shards cover the area you
need, use them instead of re-reading files. Only fall back to other tools for
code regions that have no shards yet.

**While exploring code:** When you read something important, create a shard.
If you'd want to remember it next session, shard it now. Good candidates:
- Key functions, entry points, and core logic
- Non-obvious patterns, conventions, or architectural decisions
- Tricky code that required effort to understand

**When answering questions about code you've seen before:** Check your tags
first, then view the relevant shards. Don't re-read files you already have shards for.

## How it works
- glance://tags resource: Shows top 20 tags by recent activity. Start here.
- view_shards(tags, file): Load shards by tag or file. At least one filter required.
- search_tags: Fuzzy search to discover tags by name.
- create_shard: Bookmark a code region. Upserts on file+from_text match.
- delete_tag: Remove a tag. Orphaned shards (no tags left) are deleted.

## Tips
- Use tags to organize by feature, system, or task
- Add summaries for complex code where your interpretation saves future context
- Skip summaries when the code speaks for itself
- When view_shards shows stale shards, re-create them to refresh or let them expire
""",
)

store = ShardStore(PROJECT_ROOT)


@mcp.tool()
def create_shard(
    file: str,
    from_text: str,
    to_text: str,
    tags: list[str],
    summary: Optional[str] = None,
) -> str:
    """Save a live window into a code region as a memory shard.

    The shard points to the region between from_text and to_text in the file.
    On future views, the live content at that location is shown, and health
    is tracked to detect when code has changed.

    If a shard already exists for the same file+from_text, it is overwritten
    (upsert behavior), which also resets its health. Use this to refresh
    stale shards.

    Args:
        file: Path to the source file (relative to project root)
        from_text: Text marking the start of the region (e.g. "def process_upload(")
        to_text: Text marking the end of the region (e.g. "return response")
        tags: Tags for organizing and querying shards (e.g. ["auth", "api"])
        summary: Optional summary. If provided, this is shown instead of raw
                 content when the shard is healthy. Use summaries when your
                 interpretation of the code is more useful than the code itself.
                 Summaries should capture everything relevant about the code chunk.
                 Skip summaries when the code is already concise and self-explanatory.
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

    # Create the shard
    anchor = ShardAnchor(
        from_text=from_text,
        to_text=to_text,
        function_anchor=region.function_anchor,
        start_line=region.start_line,
        end_line=region.end_line,
    )

    shard = Shard(
        file=file,
        anchor=anchor,
        original_content=region.content,
        original_hash=Shard.hash_content(region.content),
        summary=summary,
        tags=tags,
    )

    shard, was_update = store.upsert(shard)

    action = "Updated" if was_update else "Created"
    result = {
        "status": "ok",
        "action": action.lower(),
        "shard_id": shard.id,
        "file": file,
        "lines": f"{region.start_line}-{region.end_line}",
        "tags": tags,
        "has_summary": summary is not None,
    }
    if region.function_anchor:
        result["function_anchor"] = region.function_anchor

    return json.dumps(result)


DEFAULT_LIMIT = 50


@mcp.tool()
def view_shards(
    tags: Optional[list[str]] = None,
    file: Optional[str] = None,
    raw: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
) -> str:
    """View memory shards with live content and health status.

    You MUST provide at least one filter (tags or file). Use the glance://tags
    resource to discover available tags, then drill into specific ones.

    By default, shards with summaries show the summary (not raw code) to save
    context. Use raw=True to bypass summaries and see actual file content.

    Shards with low health will always show raw content regardless of the raw flag,
    since their summaries can no longer be trusted.

    After viewing, shards that are stale or broken will be flagged. You can either:
    - Re-create them with create_shard to refresh (resets health)
    - Ignore them and they'll be deleted after a few more views

    Shards are returned oldest-first (so the most recent context is closest to
    your next response). By default, at most 50 shards are returned. Use offset
    to page through older shards.

    Args:
        tags: Filter shards by tags (returns shards matching ANY tag).
        file: Filter shards by file path. Can be combined with tags.
        raw: If True, show raw file content instead of summaries for all shards.
        limit: Max shards to return (default 50). Use with offset to page through results.
        offset: Skip this many shards (oldest first) before returning. Default 0.
    """
    # Require at least one filter
    if not tags and not file:
        return json.dumps({
            "error": "Provide at least one filter (tags or file). "
            "Use search_tags() or the glance://tags resource to discover available tags."
        })

    # Gather matching shards
    if tags:
        shards = store.get_by_tags(tags)
        if file:
            file_path = _resolve_file_path(file)
            shards = [s for s in shards if s.file == file or _resolve_file_path(s.file) == file_path]
    else:
        file_path = _resolve_file_path(file)
        shards = store.get_by_file(file) or store.get_by_file(file_path)

    if not shards:
        filter_desc = []
        if tags:
            filter_desc.append(f"tags={tags}")
        if file:
            filter_desc.append(f"file={file}")
        filter_str = ", ".join(filter_desc) if filter_desc else "any"
        return json.dumps({
            "status": "empty",
            "message": f"No shards found matching {filter_str}",
            "⚠ IMPORTANT": "If you explore this area manually, you MUST call create_shard() "
            "to save what you learn. Without shards, this knowledge is lost when the session ends."
        })

    # Sort oldest-first so most recent context is closest to the LLM's next response
    shards.sort(key=lambda s: s.created_at)

    # Paginate
    effective_limit = limit if limit is not None else DEFAULT_LIMIT
    total_matching = len(shards)
    shards = shards[offset:offset + effective_limit]

    results = []
    flagged_for_deletion = []
    to_delete_now = []

    for shard in shards:
        # Resolve live content
        file_path = _resolve_file_path(shard.file)
        region = resolve_region(
            file_path,
            shard.anchor.from_text,
            shard.anchor.to_text,
            function_anchor=shard.anchor.function_anchor,
        )

        current_content = region.content if region else None
        health = assess_shard(shard, current_content)

        # Build the view
        entry: dict = {
            "shard_id": shard.id,
            "file": shard.file,
            "tags": shard.tags,
            "health": {"score": health.score, "status": health.status, "message": health.message},
        }

        if region:
            entry["lines"] = f"{region.start_line}-{region.end_line}"

        # Decide what content to show
        if raw or not health.should_show_summary():
            # Show raw content
            entry["content"] = current_content or "[Could not resolve]"
            if shard.summary and not raw:
                entry["note"] = "Summary bypassed due to low health — showing raw content"
            if not shard.summary and health.status in ("degraded", "stale"):
                entry["💡 tip"] = (
                    "This shard has no summary and the code has changed. "
                    "Re-create it with create_shard() to refresh, and consider adding a summary."
                )
        else:
            # Show summary if available, otherwise raw
            if shard.summary:
                entry["summary"] = shard.summary
            else:
                entry["content"] = current_content or "[Could not resolve]"
                entry["💡 tip"] = (
                    "This shard has no summary. Consider re-creating it with a summary "
                    "to save context in future sessions: create_shard(..., summary=\"...\")"
                )

        # Track stale shards
        if health.should_delete():
            to_delete_now.append(shard.id)
            entry["⚠ action_required"] = "This shard has expired and will be deleted."
        elif health.should_flag_deletion():
            flagged_for_deletion.append(shard.id)
            store.increment_stale_views(shard.id)

        results.append(entry)

    # Track last_viewed for all viewed shards (excluding ones about to be deleted)
    viewed_ids = [s.id for s in shards if s.id not in set(to_delete_now)]
    if viewed_ids:
        store.update_last_viewed(viewed_ids)

    # Delete expired shards
    if to_delete_now:
        store.delete_many(to_delete_now)

    # Build response
    response: dict = {"shards": results, "count": len(results), "total": total_matching}
    if total_matching > offset + effective_limit:
        remaining = total_matching - (offset + effective_limit)
        next_offset = offset + effective_limit
        response["more"] = (
            f"{remaining} older shard(s) not shown. "
            f"Use view_shards(..., offset={next_offset}) to see more."
        )

    if flagged_for_deletion:
        response["⚠ attention"] = (
            f"Shards {flagged_for_deletion} have low confidence and will be "
            f"deleted soon unless you re-create them with create_shard. "
            f"Use view_shards(raw=True) to inspect their current content."
        )

    if to_delete_now:
        response["🗑 deleted"] = (
            f"Shards {to_delete_now} were expired and have been deleted. "
            f"Re-explore these areas and create new shards if still needed."
        )

    return json.dumps(response, indent=2)


@mcp.tool()
def search_tags(query: str) -> str:
    """Search for tags by name (fuzzy substring match).

    Returns up to 5 matching tags with shard counts, sorted by relevance.
    Use this to discover what tags exist before querying shards.

    Args:
        query: Search string to match against tag names.
    """
    query_lower = query.lower()
    tag_map = store.get_all_tags()

    scored = []
    for tag, shards in tag_map.items():
        tag_lower = tag.lower()
        if query_lower in tag_lower:
            # Exact match scores highest, then prefix, then substring
            if tag_lower == query_lower:
                score = 0
            elif tag_lower.startswith(query_lower):
                score = 1
            else:
                score = 2
            scored.append((score, tag, len(shards)))

    scored.sort(key=lambda x: (x[0], -x[2]))
    results = [{"tag": tag, "shard_count": count} for _, tag, count in scored[:5]]

    if not results:
        return json.dumps({
            "results": [],
            "⚠ IMPORTANT": "No shards matched this search. If you explore code manually "
            "after this (using Read, Grep, Glob, etc.), you MUST call create_shard() to "
            "save what you learn. Without shards, this knowledge is lost when the session ends. "
            "Every important function, pattern, or decision you discover should become a shard."
        })

    return json.dumps(results)


@mcp.tool()
def delete_tag(tag: str) -> str:
    """Remove a tag from all shards. Shards left with no tags are deleted.

    Args:
        tag: The tag to remove.
    """
    modified, orphans = store.remove_tag(tag)
    if modified == 0:
        return json.dumps({"status": "not_found", "message": f"No shards have tag '{tag}'"})
    return json.dumps({
        "status": "ok",
        "tag": tag,
        "shards_modified": modified,
        "orphans_deleted": orphans,
    })


@mcp.resource("glance://tags")
def tags_resource() -> str:
    """Top tags ranked by most recent view activity."""
    tag_map = store.get_all_tags()

    def _rank_key(shards: list) -> str:
        """Return the most recent last_viewed (or updated_at) across shards."""
        timestamps = []
        for s in shards:
            timestamps.append(s.last_viewed or s.updated_at)
        return max(timestamps) if timestamps else ""

    if not tag_map:
        return json.dumps({
            "status": "empty",
            "message": "No shards exist yet for this codebase. "
            "As you explore code, use create_shard() to bookmark important regions "
            "(key functions, tricky logic, architectural decisions). "
            "Future sessions will have instant context instead of re-reading files."
        })

    ranked = sorted(tag_map.items(), key=lambda item: _rank_key(item[1]), reverse=True)
    results = [{"tag": tag, "shard_count": len(shards)} for tag, shards in ranked[:20]]
    return json.dumps(results)


def _resolve_file_path(file: str) -> str:
    """Resolve a file path relative to the project root."""
    path = Path(file)
    if path.is_absolute():
        return str(path)
    return str(Path(PROJECT_ROOT).resolve() / file)
