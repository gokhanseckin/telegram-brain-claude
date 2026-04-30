"""Weekly Review system prompt — spec §5.3. Batch API."""

from __future__ import annotations

from tbc_common.db.models import Tag
from tbc_common.db.tags import render_tag_guidance


def build_weekly_system(tags: list[Tag]) -> str:
    guidance = render_tag_guidance(tags) if tags else ""
    return _WEEKLY_TEMPLATE.format(tag_guidance=guidance)


_WEEKLY_TEMPLATE = """\
Write the user's Weekly Review. Goals:
1. Pattern recognition across the week that daily briefs couldn't see.
2. Honest assessment of where the user's attention went vs where business value is.
3. Specific, concrete recommendations for the coming week.

Sections:

A. WHERE YOUR ATTENTION WENT — data-driven, cite message volume by chat tag.
B. WHERE VALUE MOVED — deals progressed, stalled, lost, or opened.
C. PATTERNS — recurring objections, themes across clients, things said more than once.
D. MISSED OR AT-RISK — what slipped this week and why.
E. NEXT WEEK'S PRIORITIES — 3-5 specific, named actions.

Be willing to say uncomfortable things. If the user is avoiding a deal, name it.
If two clients are circling the same concern, connect them. If the pipeline looks
thin, say so plainly.

Max length: ~6000 chars.

{tag_guidance}
"""
