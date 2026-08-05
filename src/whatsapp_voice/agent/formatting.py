"""Normalise model output for WhatsApp's renderer.

The system prompt asks for WhatsApp-flavoured formatting, and both models mostly
comply — but "mostly" is the wrong guarantee for a mechanical transformation.
Models leak `**bold**` and `-` bullets under pressure, and WhatsApp shows those
as literal punctuation the user has to read past.

So this runs unconditionally on every reply. Prompting reduces how often there is
anything to fix; this makes it not matter.

WhatsApp's actual markup: `*bold*`, `_italic_`, `~strike~`, ```` ``` ```` for
code. No headings, no markdown lists, no links with bracket syntax.
"""

from __future__ import annotations

import re

#: `**bold**` → `*bold*`. Non-greedy, and requires content, so `****` is left alone.
_DOUBLE_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
#: Markdown headings at line start — WhatsApp shows the hashes.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
#: `- item` / `* item` / `+ item` at line start → `• item`.
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?=\S)", re.MULTILINE)
#: Three or more newlines collapse to two — WhatsApp already spaces paragraphs.
_EXTRA_BLANKS = re.compile(r"\n{3,}")


def for_whatsapp(text: str) -> str:
    """Rewrite markdown the renderer would show literally.

    Order matters: `**` is collapsed before the bullet rule runs, so a line like
    `- **Sent:** 2,841` does not have its leading `*` mistaken for a bullet
    marker on the second pass.
    """
    if not text:
        return text

    text = _DOUBLE_BOLD.sub(r"*\1*", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _EXTRA_BLANKS.sub("\n\n", text)
    return text.strip()
