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
    instructions="""Glance is a memory system that lets you save live windows into code.

Instead of writing notes about code, you save "shards" — pointers to specific
regions of files that resolve to live content every time you view them.

## Quick start
1. While exploring code, use create_shard to bookmark important regions
2. In later sessions, use view_shards to recall what you learned
3. If shards have degraded, re-create them to refresh your memory

## Tips
- Use tags to organize shards by feature, system, or task
- Add summaries for complex code where your interpretation is more useful than the raw code
- Skip summaries when the code speaks for itself
- When view_shards shows stale shards, either re-create them or let them expire
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


@mcp.tool()
def view_shards(
    tags: Optional[list[str]] = None,
    file: Optional[str] = None,
    raw: bool = False,
) -> str:
    """View memory shards with live content and health status.

    By default, shards with summaries show the summary (not raw code) to save
    context. Use raw=True to bypass summaries and see actual file content.

    Shards with low health will always show raw content regardless of the raw flag,
    since their summaries can no longer be trusted.

    After viewing, shards that are stale or broken will be flagged. You can either:
    - Re-create them with create_shard to refresh (resets health)
    - Ignore them and they'll be deleted after a few more views

    Args:
        tags: Filter shards by tags (returns shards matching ANY tag). If None, returns all shards.
        file: Filter shards by file path. Can be combined with tags.
        raw: If True, show raw file content instead of summaries for all shards.
    """
    # Gather matching shards
    if tags:
        shards = store.get_by_tags(tags)
        if file:
            file_path = _resolve_file_path(file)
            shards = [s for s in shards if s.file == file or _resolve_file_path(s.file) == file_path]
    elif file:
        file_path = _resolve_file_path(file)
        shards = store.get_by_file(file) or store.get_by_file(file_path)
    else:
        shards = store.get_all()

    if not shards:
        filter_desc = []
        if tags:
            filter_desc.append(f"tags={tags}")
        if file:
            filter_desc.append(f"file={file}")
        filter_str = ", ".join(filter_desc) if filter_desc else "any"
        return json.dumps({"status": "empty", "message": f"No shards found matching {filter_str}"})

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
        else:
            # Show summary if available, otherwise raw
            if shard.summary:
                entry["summary"] = shard.summary
            else:
                entry["content"] = current_content or "[Could not resolve]"

        # Track stale shards
        if health.should_delete():
            to_delete_now.append(shard.id)
            entry["⚠ action_required"] = "This shard has expired and will be deleted."
        elif health.should_flag_deletion():
            flagged_for_deletion.append(shard.id)
            store.increment_stale_views(shard.id)

        results.append(entry)

    # Delete expired shards
    if to_delete_now:
        store.delete_many(to_delete_now)

    # Build response
    response: dict = {"shards": results, "count": len(results)}

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


def _resolve_file_path(file: str) -> str:
    """Resolve a file path relative to the project root."""
    path = Path(file)
    if path.is_absolute():
        return str(path)
    return str(Path(PROJECT_ROOT).resolve() / file)
