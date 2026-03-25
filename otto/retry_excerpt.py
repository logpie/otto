"""Extract failure-relevant excerpts from test output for retry prompts.

Keeps failure blocks + summary, drops passing test noise. Framework-agnostic:
works with jest, pytest, vitest, go test, cargo test.
"""

import re

# Strip ANSI escape sequences
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")

# Lines that indicate a failure — keep a window around these
_FAIL_ANCHOR_RE = re.compile(
    r"^\s*(?:"
    r"FAIL(?:ED)?\b|ERROR\b|"
    r"Traceback \(most recent call last\):|"
    r"panic:\s|thread '.*' panicked at|"
    r"--- FAIL:|not ok\b|"
    r"(?:Assertion|Type|Reference|Syntax|Import|Module(?:NotFound)?)Error\b|"
    r">\s+assert\b"
    r")",
    re.IGNORECASE,
)

# Summary lines at the end — always keep
_SUMMARY_RE = re.compile(
    r"^\s*(?:"
    r"=+\s.*?(?:failed|error|passed).*=+|"  # pytest
    r"Test Suites:\s|Tests:\s|Snapshots:\s|"  # jest/vitest
    r"Ran all test suites|"
    r"FAIL\t|"  # go test
    r"failures:\s*$|test result:\s+FAILED\b|error: test failed\b"  # cargo
    r")",
    re.IGNORECASE,
)

# Passing test noise — collapse these
_PASS_NOISE_RE = re.compile(
    r"^\s*(?:PASS\s|✓\s|ok\s+\d|ok\t|\.{5,})",
    re.IGNORECASE,
)

# Console warnings / act() warnings — noise
_WARN_NOISE_RE = re.compile(
    r"^\s*(?:console\.warn|"
    r"An update to .* inside a test was not wrapped in act|"
    r"When testing, code that causes React state|"
    r"act\(\(\) =>|/\* fire events|/\* assert on|"
    r"This ensures that you|"
    r"\(node:\d+\)\s*\[DEP|"
    r"\(Use .node --trace-deprecation|"
    r"●\s+Console)",
    re.IGNORECASE,
)

_ACT_WARNING_RE = re.compile(r"(?:An update to .*|wrapped in act)", re.IGNORECASE)
_FAILURE_BLOCK_END_RE = re.compile(
    r"^(?:"
    r"PASS\b|FAIL\b|ok\t|"
    r"Test Suites:\s|Tests:\s|Snapshots:\s|Ran all test suites\.?|"
    r"FAIL\t|failures:\s*$|test result:\s+FAILED\b|error: test failed\b|"
    r"={20,}|-{20,}"
    r")",
    re.IGNORECASE,
)
_MAX_FAILURE_BLOCK_EXTENSION = 200


def _find_warn_noise_lines(lines: list[str]) -> set[int]:
    """Return warning/noise line indices, including multi-line act() warnings."""
    noise: set[int] = set()

    for i, line in enumerate(lines):
        if _WARN_NOISE_RE.match(line):
            noise.add(i)

        if (
            re.match(r"^\s*console\.error\s*$", line, re.IGNORECASE)
            and i + 1 < len(lines)
            and _ACT_WARNING_RE.search(lines[i + 1])
        ):
            j = i
            while j < len(lines):
                next_line = lines[j]
                if j > i and next_line and not next_line.startswith((" ", "\t")):
                    break
                if j > i and not next_line.strip():
                    break
                noise.add(j)
                j += 1

    return noise


def _extend_failure_window(lines: list[str], anchors: list[int], start: int, end: int, keep_after: int) -> tuple[int, int]:
    """Grow a failure window until we hit a clear delimiter for the block."""
    limit = len(lines) - 1

    while True:
        new_end = end
        anchor_limit = end
        for anchor in anchors:
            if start <= anchor <= end:
                new_end = max(new_end, min(limit, anchor + keep_after))
                anchor_limit = max(anchor_limit, min(limit, anchor + _MAX_FAILURE_BLOCK_EXTENSION))

        while new_end + 1 <= anchor_limit and not _FAILURE_BLOCK_END_RE.match(lines[new_end + 1]):
            new_end += 1

        if new_end == end:
            return start, end
        end = new_end


def _merge_windows(windows: list[tuple[int, int]], merge_distance: int) -> list[tuple[int, int]]:
    """Merge overlapping or nearby windows."""
    if not windows:
        return []

    merged: list[list[int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1] + merge_distance:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    return [(start, end) for start, end in merged]


def build_retry_excerpt(
    text: str,
    max_chars: int = 10_000,
    max_lines: int = 180,
) -> str:
    """Extract failure-relevant excerpt from test/build output.

    Keeps: failure blocks (with context), summary lines.
    Drops: passing test lines, console warnings, repeated stack frames.
    """
    if not text:
        return text

    # Strip ANSI
    clean = _ANSI_RE.sub("", text)
    lines = clean.splitlines()
    warn_noise = _find_warn_noise_lines(lines)

    # If already small AND no significant noise, return as-is
    has_noise = any(_PASS_NOISE_RE.match(l) or i in warn_noise for i, l in enumerate(lines[:200]))
    if len(clean) <= max_chars and len(lines) <= max_lines and not has_noise:
        return clean

    # Mark lines by type
    KEEP_BEFORE = 4  # context lines before a failure anchor
    KEEP_AFTER = 25  # lines after a failure anchor (stack trace, details)
    MERGE_DISTANCE = 50
    keep = set()  # line indices to keep

    anchors = [i for i, line in enumerate(lines) if _FAIL_ANCHOR_RE.search(line)]
    windows = [
        _extend_failure_window(
            lines,
            anchors,
            max(0, i - KEEP_BEFORE),
            min(len(lines) - 1, i + KEEP_AFTER),
            KEEP_AFTER,
        )
        for i in anchors
    ]
    windows = _merge_windows(windows, MERGE_DISTANCE)
    windows = [_extend_failure_window(lines, anchors, start, end, KEEP_AFTER) for start, end in windows]
    windows = _merge_windows(windows, MERGE_DISTANCE)

    for start, end in windows:
        for j in range(start, end + 1):
            keep.add(j)

    for i, line in enumerate(lines):
        if _SUMMARY_RE.search(line):
            for j in range(max(0, i - 1), min(len(lines), i + 3)):
                keep.add(j)

    # If no failures found, keep head + tail
    if not keep:
        head = lines[:30]
        tail = lines[-30:]
        omitted = len(lines) - 60
        if omitted > 0:
            result = "\n".join(head) + f"\n\n... {omitted} lines omitted ...\n\n" + "\n".join(tail)
        else:
            result = clean
        return result[:max_chars]

    # Build output from kept lines, collapsing gaps
    result_lines: list[str] = []
    prev_kept = -2  # tracks gap detection
    noise_collapsed = 0

    for i, line in enumerate(lines):
        if i in keep:
            # Skip noise even within kept windows
            if _PASS_NOISE_RE.match(line) or i in warn_noise:
                noise_collapsed += 1
                continue
            if noise_collapsed > 0:
                result_lines.append(f"  ... {noise_collapsed} passing/warning lines omitted ...")
                noise_collapsed = 0
            if i > prev_kept + 1 and prev_kept >= 0:
                gap = i - prev_kept - 1
                result_lines.append(f"\n... {gap} lines omitted ...\n")
            result_lines.append(line)
            prev_kept = i
        else:
            if _PASS_NOISE_RE.match(line) or i in warn_noise:
                noise_collapsed += 1

    if noise_collapsed > 0:
        result_lines.append(f"  ... {noise_collapsed} passing/warning lines omitted ...")

    result = "\n".join(result_lines)

    # Hard cap
    if len(result) > max_chars:
        # Keep first half + last half
        half = max_chars // 2
        result = result[:half] + f"\n\n... truncated ({len(result) - max_chars} chars) ...\n\n" + result[-half:]

    return result
