"""End-to-end test for Glance memory system."""

import json
import os
import tempfile
from pathlib import Path

# Set project root before importing glance
tmpdir = tempfile.mkdtemp()
os.environ["GLANCE_PROJECT_ROOT"] = tmpdir

from glance.health import compute_health
from glance.models import Shard, ShardAnchor
from glance.resolver import resolve_region, detect_function_name
from glance.storage import ShardStore


def setup_test_file(tmpdir: str) -> str:
    """Create a test Python file."""
    test_file = Path(tmpdir) / "example.py"
    test_file.write_text("""\
import os

def process_upload(file_data, user_id):
    \"\"\"Process an uploaded file.\"\"\"
    validated = validate_file(file_data)
    if not validated:
        raise ValueError("Invalid file")
    
    result = save_to_storage(validated, user_id)
    log_upload(user_id, result.id)
    return result

def validate_file(file_data):
    \"\"\"Check file type and size.\"\"\"
    if file_data.size > MAX_SIZE:
        return None
    if file_data.type not in ALLOWED_TYPES:
        return None
    return file_data

class AuthMiddleware:
    def __init__(self, secret_key):
        self.secret_key = secret_key
    
    def verify_token(self, token):
        \"\"\"Verify a JWT token.\"\"\"
        try:
            payload = jwt.decode(token, self.secret_key)
            return payload["user_id"]
        except jwt.ExpiredSignatureError:
            raise AuthError("Token expired")
        except jwt.InvalidTokenError:
            raise AuthError("Invalid token")
""")
    return str(test_file)


def test_resolver():
    """Test region resolution."""
    print("=== Testing Resolver ===")
    
    file_path = setup_test_file(tmpdir)
    
    # Test: resolve a function by from/to text
    region = resolve_region(file_path, "def process_upload(", "return result")
    assert region is not None, "Should resolve process_upload"
    assert "process_upload" in region.content
    assert "return result" in region.content
    assert region.function_anchor == "process_upload"
    print(f"  ✓ Resolved process_upload at lines {region.start_line}-{region.end_line}")
    
    # Test: resolve a method inside a class
    region = resolve_region(file_path, "def verify_token(", "raise AuthError")
    assert region is not None, "Should resolve verify_token"
    assert "verify_token" in region.content
    print(f"  ✓ Resolved verify_token at lines {region.start_line}-{region.end_line}")
    
    # Test: function name detection
    assert detect_function_name("def process_upload(file_data, user_id):") == "process_upload"
    assert detect_function_name("async def fetch_data(url):") == "fetch_data"
    assert detect_function_name("    def verify_token(self, token):") == "verify_token"
    print("  ✓ Function name detection works")
    
    print()


def test_health():
    """Test health computation."""
    print("=== Testing Health ===")
    
    original = "def foo():\n    return 42\n"
    
    # Identical
    score = compute_health(original, original)
    assert score == 1.0, f"Identical should be 1.0, got {score}"
    print(f"  ✓ Identical content: {score}")
    
    # Minor edit
    minor_edit = "def foo():\n    return 43\n"
    score = compute_health(original, minor_edit)
    assert score > 0.8, f"Minor edit should be >0.8, got {score}"
    print(f"  ✓ Minor edit: {score}")
    
    # Major rewrite
    rewrite = "class Bar:\n    def __init__(self):\n        self.x = 'totally different'\n"
    score = compute_health(original, rewrite)
    assert score < 0.4, f"Major rewrite should be <0.4, got {score}"
    print(f"  ✓ Major rewrite: {score}")
    
    # Whitespace only
    whitespace = "def foo():\n    return 42"  # removed trailing newline
    score = compute_health(original, whitespace)
    assert score > 0.95, f"Whitespace change should be >0.95, got {score}"
    print(f"  ✓ Whitespace-only change: {score}")
    
    print()


def test_storage():
    """Test shard storage with upsert."""
    print("=== Testing Storage ===")
    
    store = ShardStore(tmpdir)
    
    # Create a shard
    shard = Shard(
        file="example.py",
        anchor=ShardAnchor(from_text="def process_upload(", to_text="return result"),
        original_content="def process_upload(...):\n    ...\n    return result",
        original_hash=Shard.hash_content("def process_upload(...):\n    ...\n    return result"),
        summary="Processes uploaded files: validates, saves to storage, logs, returns result.",
        tags=["upload", "api"],
    )
    shard, was_update = store.upsert(shard)
    assert not was_update, "First insert should not be an update"
    shard_id = shard.id
    print(f"  ✓ Created shard {shard_id}")
    
    # Query by tag
    results = store.get_by_tags(["upload"])
    assert len(results) == 1
    assert results[0].id == shard_id
    print(f"  ✓ Found shard by tag 'upload'")
    
    # Query by multiple tags
    results = store.get_by_tags(["api", "auth"])
    assert len(results) == 1  # matches 'api'
    print(f"  ✓ Found shard by multi-tag query")
    
    # Upsert: same from_text should overwrite
    updated_shard = Shard(
        file="example.py",
        anchor=ShardAnchor(from_text="def process_upload(", to_text="return result"),
        original_content="def process_upload(...):\n    ...\n    return result\n# updated",
        original_hash=Shard.hash_content("def process_upload(...):\n    ...\n    return result\n# updated"),
        summary="Updated summary.",
        tags=["upload", "api", "v2"],
    )
    updated_shard, was_update = store.upsert(updated_shard)
    assert was_update, "Second insert should be an update"
    assert updated_shard.id == shard_id, "Should preserve original ID"
    print(f"  ✓ Upserted shard (preserved ID {shard_id})")
    
    # Verify only one shard exists
    all_shards = store.get_all()
    assert len(all_shards) == 1, f"Should have 1 shard, got {len(all_shards)}"
    assert all_shards[0].tags == ["upload", "api", "v2"]
    print(f"  ✓ Upsert replaced, not duplicated")
    
    print()


def test_end_to_end():
    """Test the full create → view → modify → view cycle."""
    print("=== Testing End-to-End ===")
    
    file_path = setup_test_file(tmpdir)
    store = ShardStore(tmpdir)
    
    # Simulate: agent explores and creates a shard
    from glance.resolver import resolve_region as rr
    region = rr(file_path, "def process_upload(", "return result")
    assert region is not None
    
    shard = Shard(
        file=str(Path(file_path)),
        anchor=ShardAnchor(
            from_text="def process_upload(",
            to_text="return result",
            function_anchor=region.function_anchor,
            start_line=region.start_line,
            end_line=region.end_line,
        ),
        original_content=region.content,
        original_hash=Shard.hash_content(region.content),
        summary="Processes file uploads: validates, stores, logs, returns result.",
        tags=["upload"],
    )
    store.upsert(shard)
    print("  ✓ Agent created shard during exploration")
    
    # Simulate: view the shard (no changes)
    from glance.health import assess_shard
    current = rr(file_path, "def process_upload(", "return result")
    health = assess_shard(shard, current.content)
    assert health.status == "healthy"
    assert health.should_show_summary()
    print(f"  ✓ Shard is healthy (score={health.score}), showing summary")
    
    # Simulate: someone modifies the file slightly
    content = Path(file_path).read_text()
    content = content.replace(
        "log_upload(user_id, result.id)",
        "log_upload(user_id, result.id)\n    notify_user(user_id, result)"
    )
    Path(file_path).write_text(content)
    print("  ✓ File modified (added notify_user call)")
    
    # View again — should be slightly degraded but still healthy
    current = rr(file_path, "def process_upload(", "return result")
    health = assess_shard(shard, current.content)
    print(f"  ✓ After minor edit: status={health.status}, score={health.score}")
    
    # Simulate: major rewrite
    content = Path(file_path).read_text()
    content = content.replace(
        """def process_upload(file_data, user_id):
    \"\"\"Process an uploaded file.\"\"\"
    validated = validate_file(file_data)
    if not validated:
        raise ValueError("Invalid file")
    
    result = save_to_storage(validated, user_id)
    log_upload(user_id, result.id)
    notify_user(user_id, result)
    return result""",
        """def process_upload(request):
    \"\"\"Completely rewritten upload handler using new framework.\"\"\"
    async with DatabaseSession() as db:
        record = await db.create_upload(request.body)
        await EventBus.publish("upload.created", record)
        return JSONResponse(record.to_dict())"""
    )
    Path(file_path).write_text(content)
    print("  ✓ File majorly rewritten")
    
    current = rr(file_path, "def process_upload(", "return JSON")
    if current:
        health = assess_shard(shard, current.content)
        print(f"  ✓ After major rewrite: status={health.status}, score={health.score}")
        assert health.score < 0.5, "Major rewrite should have low health"
        assert not health.should_show_summary(), "Summary should not be trusted"
    else:
        print("  ✓ After major rewrite: region could not be resolved (expected)")
    
    print()


def test_last_viewed():
    """Test that last_viewed is tracked."""
    print("=== Testing Last Viewed ===")

    store = ShardStore(tmpdir)
    # Clear existing shards
    for s in store.get_all():
        store.delete(s.id)

    shard = Shard(
        file="example.py",
        anchor=ShardAnchor(from_text="def foo(", to_text="return bar"),
        original_content="def foo():\n    return bar",
        original_hash=Shard.hash_content("def foo():\n    return bar"),
        tags=["test"],
    )
    shard, _ = store.upsert(shard)
    assert shard.last_viewed is None, "last_viewed should be None initially"
    print("  ✓ last_viewed is None on creation")

    store.update_last_viewed([shard.id])
    updated = store.get_all()[0]
    assert updated.last_viewed is not None, "last_viewed should be set after update"
    print(f"  ✓ last_viewed set to {updated.last_viewed}")

    print()


def test_search_tags():
    """Test tag search."""
    print("=== Testing Search Tags ===")

    store = ShardStore(tmpdir)
    # Clear existing shards
    for s in store.get_all():
        store.delete(s.id)

    # Create shards with various tags
    for i, tags in enumerate([["auth", "api"], ["auth", "middleware"], ["upload", "api"]]):
        s = Shard(
            file=f"file{i}.py",
            anchor=ShardAnchor(from_text=f"def f{i}(", to_text=f"return {i}"),
            original_content=f"content{i}",
            original_hash=Shard.hash_content(f"content{i}"),
            tags=tags,
        )
        store.upsert(s)

    tag_map = store.get_all_tags()

    # Exact match
    assert "auth" in tag_map
    assert len(tag_map["auth"]) == 2
    print("  ✓ 'auth' has 2 shards")

    assert "api" in tag_map
    assert len(tag_map["api"]) == 2
    print("  ✓ 'api' has 2 shards")

    assert "upload" in tag_map
    assert len(tag_map["upload"]) == 1
    print("  ✓ 'upload' has 1 shard")

    # Test search via server tool
    from glance.server import search_tags
    result = json.loads(search_tags("auth"))
    assert len(result) == 1
    assert result[0]["tag"] == "auth"
    assert result[0]["shard_count"] == 2
    print("  ✓ search_tags('auth') returns correct result")

    # Substring match
    result = json.loads(search_tags("api"))
    assert any(r["tag"] == "api" for r in result)
    print("  ✓ search_tags('api') finds 'api'")

    # No match
    result = json.loads(search_tags("nonexistent"))
    assert len(result) == 0
    print("  ✓ search_tags('nonexistent') returns empty")

    print()


def test_delete_tag():
    """Test tag deletion and orphan cleanup."""
    print("=== Testing Delete Tag ===")

    store = ShardStore(tmpdir)
    # Clear existing shards
    for s in store.get_all():
        store.delete(s.id)

    # Create shards: one with multiple tags, one with single tag
    multi = Shard(
        file="multi.py",
        anchor=ShardAnchor(from_text="def multi(", to_text="return multi"),
        original_content="multi content",
        original_hash=Shard.hash_content("multi content"),
        tags=["shared", "keep"],
    )
    single = Shard(
        file="single.py",
        anchor=ShardAnchor(from_text="def single(", to_text="return single"),
        original_content="single content",
        original_hash=Shard.hash_content("single content"),
        tags=["shared"],
    )
    store.upsert(multi)
    store.upsert(single)
    assert len(store.get_all()) == 2
    print("  ✓ Created 2 shards")

    # Delete 'shared' tag — should modify both, orphan-delete the single-tag one
    modified, orphans = store.remove_tag("shared")
    assert modified == 2, f"Expected 2 modified, got {modified}"
    assert orphans == 1, f"Expected 1 orphan, got {orphans}"
    print(f"  ✓ remove_tag('shared'): modified={modified}, orphans={orphans}")

    remaining = store.get_all()
    assert len(remaining) == 1, f"Expected 1 remaining, got {len(remaining)}"
    assert remaining[0].tags == ["keep"]
    print("  ✓ Only shard with remaining tags survives")

    # Delete non-existent tag
    from glance.server import delete_tag
    result = json.loads(delete_tag("nonexistent"))
    assert result["status"] == "not_found"
    print("  ✓ delete_tag('nonexistent') returns not_found")

    print()


def test_tags_resource():
    """Test the glance://tags resource."""
    print("=== Testing Tags Resource ===")

    store = ShardStore(tmpdir)
    # Clear existing shards
    for s in store.get_all():
        store.delete(s.id)

    # Create some shards with different tags and last_viewed
    s1 = Shard(
        file="a.py",
        anchor=ShardAnchor(from_text="def a(", to_text="return a"),
        original_content="a",
        original_hash=Shard.hash_content("a"),
        tags=["old-tag"],
        last_viewed="2024-01-01T00:00:00+00:00",
    )
    s2 = Shard(
        file="b.py",
        anchor=ShardAnchor(from_text="def b(", to_text="return b"),
        original_content="b",
        original_hash=Shard.hash_content("b"),
        tags=["recent-tag"],
        last_viewed="2025-06-01T00:00:00+00:00",
    )
    store.upsert(s1)
    store.upsert(s2)

    from glance.server import tags_resource
    result = json.loads(tags_resource())
    assert len(result) == 2
    # recent-tag should come first (more recent last_viewed)
    assert result[0]["tag"] == "recent-tag"
    assert result[1]["tag"] == "old-tag"
    print("  ✓ Tags ranked by last_viewed (most recent first)")

    print()


if __name__ == "__main__":
    test_resolver()
    test_health()
    test_storage()
    test_end_to_end()
    test_last_viewed()
    test_search_tags()
    test_delete_tag()
    test_tags_resource()
    print("=" * 40)
    print("All tests passed! ✓")
