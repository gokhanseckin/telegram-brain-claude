"""Rule-based pre-router. Pure-function regex matchers with no I/O.

Returns a RouterDecision when the DM is unambiguously a feedback action;
returns None for everything else (which then falls through to the LLM in
PR2 or directly to Claude in PR1).

Design choice: only the explicit-sentiment-word cases are caught here.
Free-text reactions like "#a8ce Doğa is not a prospect, he is a friend"
need an LLM to map intent and are intentionally NOT matched — silently
guessing them risks misclassification. Better to pay the latency than
pollute brief calibration with wrong rows.
"""

from __future__ import annotations

import re

from .decision import RouterDecision

# Tag pattern: 4-8 hex chars after a `#`. Brief radar tags follow this
# shape; the column is also lowercased on storage.
_TAG = r"#?(?P<ref>[0-9a-fA-F]{4,8})"

# Vocab → canonical feedback_type. Order in the regex alternation
# matters: longest forms first so "not useful" wins over "not".
_VOCAB: list[tuple[str, str]] = [
    (r"missed[ _-]?important", "missed_important"),
    (r"not[ _-]?useful", "not_useful"),
    (r"missed", "missed_important"),
    (r"useful", "useful"),
    (r"good", "useful"),
    (r"yes", "useful"),
    (r"noise", "not_useful"),
    (r"irrelevant", "not_useful"),
    (r"no", "not_useful"),
    (r"notuseful", "not_useful"),
]

_SENTIMENT_GROUP = "(?:" + "|".join(p for p, _ in _VOCAB) + ")"

# Forms accepted (case-insensitive, trailing period stripped):
#   #abcd useful
#   #abcd not useful
#   useful #abcd
#   #abcd not_useful "optional note"
#   #abcd useful makes sense
_TAG_FIRST = re.compile(
    rf"^{_TAG}\s+(?P<sentiment>{_SENTIMENT_GROUP})(?:\s+\"?(?P<note>[^\"]+)\"?)?$",
    re.IGNORECASE,
)
_SENTIMENT_FIRST = re.compile(
    rf"^(?P<sentiment>{_SENTIMENT_GROUP})\s+{_TAG}(?:\s+\"?(?P<note>[^\"]+)\"?)?$",
    re.IGNORECASE,
)

# Commitment shortcut patterns: free-text equivalents of /done c<id> and
# /cancel c<id>. The `c` prefix on the id is required to disambiguate
# from an open-ended "done with Bob" which we want to leave to Claude
# (no explicit id → no rule match → LLM/Claude path).
#
# Verbs accepted:
#   done / finished / completed / resolved   → commitment_resolve
#   cancel / cancelled / drop / forget       → commitment_cancel
_RESOLVE_VERBS = r"(?:done|finished|completed|resolved)"
_CANCEL_VERBS = r"(?:cancel(?:led|led)?|drop|forget)"

_DONE_BY_ID = re.compile(
    rf"^{_RESOLVE_VERBS}\s+c(?P<cid>\d+)(?:\s+(?P<rest>.+))?$",
    re.IGNORECASE,
)
_CANCEL_BY_ID = re.compile(
    rf"^{_CANCEL_VERBS}\s+c(?P<cid>\d+)(?:\s+(?P<rest>.+))?$",
    re.IGNORECASE,
)

# Q&A about commitments by id: "explain c9273", "what is c9273",
# "tell me about c9273 and c9275". The c<digits> token is the
# disambiguator — Qwen 3B was misclassifying these as ambiguous and
# returning the rephrase prompt instead of routing to Claude.
_QA_VERBS = (
    r"(?:explain|describe|"
    r"what(?:\s+is|\s+are|'s|s)?|"
    r"tell\s+me\s+about|"
    r"details?\s+(?:on|about)|"
    r"info\s+(?:on|about))"
)
_QA_ABOUT_COMMITMENTS = re.compile(
    rf"^{_QA_VERBS}\s+c\d+(?:\s*(?:,|and|&)\s*c\d+)*\s*\??$",
    re.IGNORECASE,
)

# Retag rule: only fires on explicit hex-ref + tag combos.
# Name-based retag ("Doğa personal") needs LLM — too ambiguous at regex level.
# Tag validation is dynamic — match any word token, then check against valid_tags.
_RETAG_TAG_FIRST = re.compile(
    rf"^{_TAG}\s+(?P<new_tag>\w+)$",
    re.IGNORECASE,
)
_RETAG_NEWTAG_FIRST = re.compile(
    rf"^(?P<new_tag>\w+)\s+{_TAG}$",
    re.IGNORECASE,
)


def _classify_sentiment(raw: str) -> str | None:
    """Map a matched sentiment phrase to a canonical feedback_type.

    The regex already enforced that `raw` matches one of the vocab
    patterns, so this lookup is for the 1:N (pattern → canonical)
    mapping. Returns None only if the regex group somehow matched
    something not in the vocab, which shouldn't happen.
    """
    normalised = raw.strip().lower().replace(" ", "_").replace("-", "_")
    normalised = re.sub(r"_+", "_", normalised).strip("_")

    for pattern, canonical in _VOCAB:
        # Re-anchor and case-insensitive match against the normalised form
        if re.fullmatch(pattern.replace(r"[ _-]?", "_?"), normalised, re.IGNORECASE):
            return canonical
    return None


_DEFAULT_VALID_TAGS = {
    "prospect", "partner", "colleague",
    "family", "personal", "ignore",
}


def match_rule(text: str, valid_tags: set[str] | None = None) -> RouterDecision | None:
    """Try to match `text` to a feedback intent via regex.

    Returns a RouterDecision with confidence=1.0 on a clean match; None
    otherwise. Caller is expected to fall through to the LLM (PR2) or
    Claude (PR1) on None.

    valid_tags: if provided, retag rules validate against this set.
    Falls back to the 9 default tags when None (e.g. in tests).
    """
    tags = valid_tags if valid_tags is not None else _DEFAULT_VALID_TAGS
    stripped = text.strip().rstrip(".")
    if not stripped:
        return None

    # Commitment shortcuts get checked first — they have an unambiguous
    # `c<digits>` token and there's no overlap with the feedback patterns
    # (which require a hex `#xxxx` ref or a sentiment word).
    m = _DONE_BY_ID.match(stripped)
    if m:
        rest = m.group("rest")
        note = rest.strip() if rest else None
        return RouterDecision(
            intent="commitment_resolve",
            confidence=1.0,
            source="rule",
            fields={"commitment_id": int(m.group("cid")), "note": note or None},
        )
    m = _CANCEL_BY_ID.match(stripped)
    if m:
        rest = m.group("rest")
        reason = rest.strip() if rest else None
        return RouterDecision(
            intent="commitment_cancel",
            confidence=1.0,
            source="rule",
            fields={"commitment_id": int(m.group("cid")), "reason": reason or None},
        )

    if _QA_ABOUT_COMMITMENTS.match(stripped):
        return RouterDecision(
            intent="qa",
            confidence=1.0,
            source="rule",
            fields={},
        )

    for pat in (_RETAG_TAG_FIRST, _RETAG_NEWTAG_FIRST):
        m = pat.match(stripped)
        if m:
            new_tag = m.group("new_tag").lower()
            if new_tag not in tags:
                continue
            return RouterDecision(
                intent="retag",
                confidence=1.0,
                source="rule",
                fields={
                    "target": m.group("ref").lower(),
                    "new_tag": new_tag,
                },
            )

    for pattern in (_TAG_FIRST, _SENTIMENT_FIRST):
        m = pattern.match(stripped)
        if not m:
            continue
        ref = m.group("ref").lower()
        feedback_type = _classify_sentiment(m.group("sentiment"))
        if feedback_type is None:
            return None
        note = m.group("note")
        cleaned_note = note.strip().strip('"') if note else None
        return RouterDecision(
            intent="feedback",
            confidence=1.0,
            source="rule",
            fields={
                "feedback_type": feedback_type,
                "item_ref": ref,
                "note": cleaned_note or None,
            },
        )

    return None
