"""Format CommandResult into Telegram MarkdownV2 messages with smart splitting."""

from __future__ import annotations

from shell_session import CommandResult

# Characters that need escaping in MarkdownV2 regular text
_MD_SPECIAL = set(r'_*[]()~`>#+-=|{}.!')
# Inside a code block (triple backtick), only ` and \ need escaping
_MD_CODE_SPECIAL = set(r'\`')

CODE_FENCE_OPEN = "```bash\n"
CODE_FENCE_CLOSE = "\n```"
FENCE_OVERHEAD = len(CODE_FENCE_OPEN) + len(CODE_FENCE_CLOSE)


def escape_md_text(text: str) -> str:
    """Escape text for MarkdownV2 outside of code blocks."""
    out: list[str] = []
    for ch in text:
        if ch in _MD_SPECIAL:
            out.append('\\')
        out.append(ch)
    return ''.join(out)


def escape_md_code(text: str) -> str:
    """Escape text for MarkdownV2 inside a code block (only ` and \\)."""
    out: list[str] = []
    for ch in text:
        if ch in _MD_CODE_SPECIAL:
            out.append('\\')
        out.append(ch)
    return ''.join(out)


def _is_safe_split(s: str, pos: int) -> bool:
    """Return True if splitting the already-escaped string at `pos`
    won't orphan a backslash escape sequence."""
    if pos <= 0 or pos >= len(s):
        return True
    # Count trailing backslashes before pos. If odd, we'd split inside an escape.
    n = 0
    i = pos - 1
    while i >= 0 and s[i] == '\\':
        n += 1
        i -= 1
    return n % 2 == 0


def _split_escaped(escaped: str, hi: int, lo: int) -> int:
    """Choose a split index in escaped[0:hi], searching backwards from hi to lo
    for the best boundary (\\n\\n > \\n > space > hard cut). Result is safe
    against splitting mid-escape."""
    if len(escaped) <= hi:
        return len(escaped)
    lo = max(1, min(lo, hi - 1))
    window = escaped[lo:hi]
    for sep in ('\n\n', '\n', ' '):
        start = len(window)
        while True:
            idx = window.rfind(sep, 0, start)
            if idx == -1:
                break
            pos = lo + idx + len(sep)
            if _is_safe_split(escaped, pos):
                return pos
            start = idx
    # Hard cut; walk back until safe
    pos = hi
    while pos > lo and not _is_safe_split(escaped, pos):
        pos -= 1
    return pos


def _render_header(result: CommandResult, auto_reset_notice: bool) -> str:
    lines: list[str] = []
    if auto_reset_notice:
        lines.append("Shell was unresponsive. Session has been reset.")
    lines.append(f"$ {result.command}")
    return "\n".join(escape_md_text(ln) for ln in lines)


def _render_footer(result: CommandResult, reset_after_timeout: bool) -> str:
    if result.timed_out:
        if result.recovery_failed:
            text = (
                f"timed out after {int(result.duration_seconds)}s; "
                "shell recovery failed, use /reset"
            )
        elif reset_after_timeout:
            text = f"timed out after {int(result.duration_seconds)}s; shell was reset"
        else:
            text = f"timed out after {int(result.duration_seconds)}s; shell recovered"
    else:
        ec = result.exit_code if result.exit_code is not None else "?"
        text = f"exit: {ec}  time: {result.duration_seconds:.2f}s"
    return escape_md_text(text)


def build_messages(
    result: CommandResult,
    chunk_hi: int = 3500,
    chunk_lo: int = 2500,
    auto_reset_notice: bool = False,
    reset_after_timeout: bool = False,
) -> list[str]:
    """Render a CommandResult into one or more MarkdownV2 message strings."""
    header = _render_header(result, auto_reset_notice)
    footer = _render_footer(result, reset_after_timeout)

    body = result.output if result.output else "(no output)"
    escaped = escape_md_code(body)

    header_full = header + "\n" if header else ""
    footer_full = "\n" + footer if footer else ""

    # Fast path: single message
    single_len = len(header_full) + FENCE_OVERHEAD + len(escaped) + len(footer_full)
    if single_len <= chunk_hi:
        return [header_full + CODE_FENCE_OPEN + escaped + CODE_FENCE_CLOSE + footer_full]

    chunks: list[str] = []
    pos = 0
    n = len(escaped)
    is_first = True

    while pos < n:
        head_overhead = len(header_full) if is_first else 0
        remaining_len = n - pos

        # Try: this is the final chunk (include footer)
        last_len = head_overhead + FENCE_OVERHEAD + remaining_len + len(footer_full)
        if last_len <= chunk_hi:
            chunk = (
                (header_full if is_first else "")
                + CODE_FENCE_OPEN
                + escaped[pos:]
                + CODE_FENCE_CLOSE
                + footer_full
            )
            chunks.append(chunk)
            break

        # Need to split: build a middle (or first) chunk without footer.
        budget = chunk_hi - head_overhead - FENCE_OVERHEAD
        if budget <= 0:
            # Header alone doesn't fit; degrade: truncate header
            budget = chunk_hi - FENCE_OVERHEAD
            head_overhead = 0
            is_first = False  # drop header
            header_full = ""

        rel_hi = min(budget, remaining_len)
        rel_lo = max(1, min(chunk_lo, rel_hi - 100))
        rel_split = _split_escaped(escaped[pos:pos + rel_hi + 1], rel_hi, rel_lo)
        split = pos + rel_split

        chunk_text = (
            (header_full if is_first else "")
            + CODE_FENCE_OPEN
            + escaped[pos:split]
            + CODE_FENCE_CLOSE
        )
        chunks.append(chunk_text)
        pos = split
        is_first = False

    return chunks


def build_error_message(text: str) -> str:
    """Build a plain-text error message (MarkdownV2 escaped)."""
    return escape_md_text(text)
