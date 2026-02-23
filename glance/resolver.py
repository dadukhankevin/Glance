from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Patterns that look like function/method definitions across common languages
FUNC_PATTERNS = [
    re.compile(r"^\s*(async\s+)?def\s+(\w+)\s*\("),  # Python
    re.compile(r"^\s*(export\s+)?(async\s+)?function\s+(\w+)\s*\("),  # JS/TS
    re.compile(r"^\s*(pub\s+)?(async\s+)?fn\s+(\w+)\s*[\(<]"),  # Rust
    re.compile(r"^\s*(public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\("),  # Java/C#
    re.compile(r"^\s*func\s+(\w+)\s*\("),  # Go
]


@dataclass
class ResolvedRegion:
    content: str
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed, inclusive
    function_anchor: Optional[str] = None


def detect_function_name(line: str) -> Optional[str]:
    """Try to extract a function name from a line that looks like a definition."""
    for pattern in FUNC_PATTERNS:
        match = pattern.match(line)
        if match:
            # Return the last captured group (the function name)
            groups = [g for g in match.groups() if g and g.strip()]
            if groups:
                return groups[-1].strip()
    return None


def find_text_in_lines(lines: list[str], text: str, start_from: int = 0) -> Optional[int]:
    """
    Find a line index where `text` appears. Searches for the text as a
    substring of any line, stripped of leading/trailing whitespace.
    Returns 0-indexed line number or None.
    """
    text_stripped = text.strip()
    for i in range(start_from, len(lines)):
        if text_stripped in lines[i].strip():
            return i
        # Also try matching the raw line
        if text_stripped in lines[i]:
            return i
    return None


def find_function_by_name(lines: list[str], func_name: str) -> Optional[int]:
    """Find a function definition line by name. Returns 0-indexed line or None."""
    for i, line in enumerate(lines):
        name = detect_function_name(line)
        if name == func_name:
            return i
    return None


def resolve_region(
    file_path: str, from_text: str, to_text: str, function_anchor: Optional[str] = None
) -> Optional[ResolvedRegion]:
    """
    Resolve a shard region in a file.

    Strategy:
    1. Try to find from_text directly
    2. If not found and we have a function_anchor, find the function and search near it
    3. Find to_text after from_text
    4. Extract the region
    """
    path = Path(file_path)
    if not path.exists():
        return None

    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return None

    lines = content.splitlines()
    if not lines:
        return None

    # Step 1: Find from_text
    start_idx = find_text_in_lines(lines, from_text)

    # Step 2: Fallback to function anchor
    if start_idx is None and function_anchor:
        func_line = find_function_by_name(lines, function_anchor)
        if func_line is not None:
            # Search for from_text near the function (within 50 lines)
            search_start = max(0, func_line - 5)
            start_idx = find_text_in_lines(lines, from_text, start_from=search_start)
            # If still not found, just use the function start
            if start_idx is None:
                start_idx = func_line

    if start_idx is None:
        return None

    # Step 3: Find to_text after start
    end_idx = find_text_in_lines(lines, to_text, start_from=start_idx)

    if end_idx is None:
        # If to_text not found, try to capture a reasonable block
        # If start looks like a function, capture until dedent or next function
        end_idx = _find_block_end(lines, start_idx)

    if end_idx is None or end_idx < start_idx:
        # Last resort: just take 20 lines from start
        end_idx = min(start_idx + 20, len(lines) - 1)

    # Step 4: Extract
    region_lines = lines[start_idx : end_idx + 1]
    region_content = "\n".join(region_lines)

    # Detect function anchor if we don't have one
    func_anchor = function_anchor
    if not func_anchor:
        func_anchor = detect_function_name(lines[start_idx])

    return ResolvedRegion(
        content=region_content,
        start_line=start_idx + 1,
        end_line=end_idx + 1,
        function_anchor=func_anchor,
    )


def _find_block_end(lines: list[str], start_idx: int) -> Optional[int]:
    """
    Try to find the end of a code block starting at start_idx.
    Uses indentation-based heuristic (works well for Python, reasonable for others).
    """
    if start_idx >= len(lines):
        return None

    start_line = lines[start_idx]
    start_indent = len(start_line) - len(start_line.lstrip())

    # If this looks like a function def, find where the body ends
    if detect_function_name(start_line):
        for i in range(start_idx + 1, min(start_idx + 200, len(lines))):
            line = lines[i]
            if not line.strip():  # skip blank lines
                continue
            current_indent = len(line) - len(line.lstrip())
            # If we've returned to the same or lesser indent, the block ended
            if current_indent <= start_indent and line.strip():
                return i - 1
        return min(start_idx + 50, len(lines) - 1)

    return None
