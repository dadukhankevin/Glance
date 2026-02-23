# Glance

**Live memory shards for AI agents.** Instead of writing notes that go stale, save pointers to actual code that resolve to live content every time you look.

## The Problem

Agent memory today is basically "write yourself READMEs." These go stale immediately because codebases change underneath them. Every new session, agents launch expensive exploration subagents to rebuild context from scratch.

## The Solution

**Shards** are live windows into code regions. An agent bookmarks `from_text → to_text` in a file, and every future view resolves the current content at that location. Health tracking detects when code has changed, and stale memories gracefully degrade and expire.

## Concepts

- **Shard**: A pointer to a region in a file. Resolved live on every view.
- **Tags**: Flexible grouping. A shard can belong to multiple features/systems.
- **Summary**: Optional compressed representation. Shown instead of raw code when healthy. Bypassed automatically when the underlying code has changed.
- **Health**: Similarity between original and current content. Healthy shards are trusted. Degraded shards show raw content. Stale shards get deleted.

## MCP Tools

### `create_shard(file, from_text, to_text, tags, summary?)`

Save a live window into a code region. If a shard already exists at the same location, it is overwritten (upsert), resetting its health.

```
create_shard(
  file="src/auth/middleware.py",
  from_text="def verify_token(",
  to_text="return user_context",
  tags=["auth", "middleware"],
  summary="Validates JWT from Authorization header, extracts user_id, attaches to request context. Raises 401 on expiry or malformed tokens."
)
```

### `view_shards(tags?, file?, raw?)`

Load matching shards with live content and health status.

```
view_shards(tags=["auth"])           # See all auth-related shards
view_shards(file="src/api/routes.py") # See all shards in a file
view_shards(tags=["auth"], raw=True)  # Bypass summaries, see raw code
```

## Health Lifecycle

1. **Healthy** (score ≥ 0.8): Code is unchanged or minimally edited. Summary is trusted.
2. **Degraded** (0.4–0.8): Notable changes. Summary is bypassed, raw content shown.
3. **Stale** (score < 0.4): Major changes. Flagged for deletion unless re-created.
4. **Expired**: Stale and viewed multiple times without refresh. Automatically deleted.

## Installation

```bash
# Install from source
pip install -e .

# Or install with uv
uv pip install -e .
```

## Configuration

Set the project root via environment variable:

```bash
export GLANCE_PROJECT_ROOT=/path/to/your/project
```

Defaults to the current working directory.

### Claude Code (`claude_code_config.json`)

```json
{
  "mcpServers": {
    "glance": {
      "command": "python",
      "args": ["-m", "glance"],
      "env": {
        "GLANCE_PROJECT_ROOT": "/path/to/your/project"
      }
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "glance": {
      "command": "python",
      "args": ["-m", "glance"],
      "env": {
        "GLANCE_PROJECT_ROOT": "/path/to/your/project"
      }
    }
  }
}
```

## Storage

Shards are stored in `.glance/shards.json` in the project root. This directory is automatically added to `.gitignore` — memories are local and personal to the agent.

## Design Principles

- **Two tools, not twenty.** `create_shard` and `view_shards` are the entire API.
- **Live by default.** Shards always resolve to current file content, never cached copies.
- **Graceful degradation.** When code changes, summaries are bypassed, content is shown raw, and stale shards expire on their own.
- **Upsert, not CRUD.** Re-creating a shard refreshes it. No explicit update/delete needed.
- **Tags over hierarchy.** A shard can belong to many features. No rigid grouping.
