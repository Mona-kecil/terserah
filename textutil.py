"""Shared text helpers: Telegram-safe markup and message splitting."""

from __future__ import annotations

import html

MESSAGE_LIMIT = 3900


def render_markup(text: str) -> str:
    """Escape for HTML while keeping fenced code blocks as <pre>."""
    segments: list[tuple[str, bool]] = []
    buf: list[str] = []
    in_code = False

    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                segments.append(("\n".join(buf), True))
                buf = []
                in_code = False
            else:
                if buf:
                    segments.append(("\n".join(buf), False))
                buf = []
                in_code = True
            continue
        buf.append(line)

    if buf:
        segments.append(("\n".join(buf), in_code))

    parts: list[str] = []
    for body, is_code in segments:
        if not body.strip():
            continue
        escaped = html.escape(body)
        parts.append(f"<pre>{escaped}</pre>" if is_code else escaped)

    return "\n".join(parts) if parts else html.escape(text)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]
