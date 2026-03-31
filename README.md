# Glance

**Live memory for AI agents.** Instead of writing notes that go stale, save insights — pointers to actual code that resolve to live content every time you look.

## The Problem

Agent memory today is basically "write yourself READMEs." These go stale immediately because codebases change underneath them. Every new session, agents launch expensive exploration subagents to rebuild context from scratch.

## The Solution

**Insights** are live windows into code regions. An agent bookmarks `from_text → to_text` in a file, and every future view resolves the current content at that location. Health tracking detects when code has changed, and stale memories gracefully degrade and expire.

## Concepts

- **Insight**: A pointer to a region in a file. Resolved live on every view.
- **Tags**: Flexible grouping. An insight can belong to multiple features/systems.
- **Health**: Similarity between original and current content. Healthy insights are trusted. Degraded insights flag that code has changed. Stale insights get deleted.

## Agent Workflow

1. **Start of session**: Check `glance://tags` resource to see what memory exists. Load relevant tags with `view_insights(tags=["..."])`.
2. **While exploring**: Create insights for important code — key functions, tricky logic, architectural decisions.
3. **When answering questions**: Check tags first, then view relevant insights. Don't re-read files you already have insights for.

## MCP Tools

### `create_insight(file, from_text, to_text, tags)`

Save a live window into a code region. If an insight already exists at the same location, it is overwritten (upsert), resetting its health.

```
create_insight(
  file="src/auth/middleware.py",
  from_text="def verify_token(",
  to_text="return user_context",
  tags=["auth", "middleware"],
)
```

### `view_insights(tags?, file?, limit?, offset?)`

Load insights filtered by tag or file. **At least one filter is required** — use the `glance://tags` resource or `search_tags()` to discover available tags first.

Insights are returned oldest-first so the most recent context is closest to the LLM's next response. Returns up to 50 insights by default; use `offset` to page through more.

```
view_insights(tags=["auth"])              # See all auth-related insights
view_insights(file="src/api/routes.py")   # See all insights in a file
view_insights(tags=["api"], offset=50)    # Page through older insights
```

### `search_tags(query)`

Fuzzy substring search on tag names. Returns up to 5 matching tags with insight counts.

```
search_tags("auth")  # → [{"tag": "auth", "insight_count": 3}]
```

### `delete_tag(tag)`

Remove a tag from all insights. Insights left with no tags are deleted (orphan cleanup).

```
delete_tag("old-feature")  # → {"insights_modified": 4, "orphans_deleted": 1}
```

### Resource: `glance://tags`

Auto-loaded context listing the top 20 tags ranked by most recent view activity. This is the starting point for agents — check it at the beginning of each session to see what memory is available.

## Health Lifecycle

1. **Healthy** (score >= 0.8): Code is unchanged or minimally edited.
2. **Degraded** (0.4-0.8): Notable changes detected.
3. **Stale** (score < 0.4): Major changes. Flagged for deletion unless re-created.
4. **Expired**: Stale and viewed multiple times without refresh. Automatically deleted.

## Installation

```bash
pip install -e .
```

### Claude Code (global — all projects)

Add glance to `~/.claude.json`. Glance automatically uses the working directory as the project root, so no per-project configuration is needed.

```json
{
  "mcpServers": {
    "glance": {
      "type": "stdio",
      "command": "glance",
      "args": []
    }
  }
}
```

Or add it programmatically:

```bash
python3 -c "
import json, os
config_path = os.path.expanduser('~/.claude.json')
with open(config_path, 'r') as f:
    data = json.load(f)
data['mcpServers']['glance'] = {
    'type': 'stdio',
    'command': 'glance',
    'args': []
}
with open(config_path, 'w') as f:
    json.dump(data, f, indent=2)
"
```

Restart Claude Code after adding. Run `/mcp` to verify it shows as connected.

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "glance": {
      "command": "glance",
      "args": []
    }
  }
}
```

### Notes

- Glance uses the working directory as the project root by default. Override with `GLANCE_PROJECT_ROOT` env var if needed.
- If your client doesn't have `~/.local/bin` on `PATH`, use the full path from `which glance`.
- Restart your client after adding the config.

## Storage

Insights are stored in `.glance/insights.json` in the project root. This directory is automatically added to `.gitignore` — memories are local and personal to the agent.

## Design Principles

- **Small surface area.** Four tools and one resource. Create, view, search, delete.
- **Live by default.** Insights always resolve to current file content, never cached copies.
- **Graceful degradation.** When code changes, stale insights expire on their own.
- **Upsert, not CRUD.** Re-creating an insight refreshes it. No explicit update/delete needed.
- **Tags over hierarchy.** An insight can belong to many features. No rigid grouping.
