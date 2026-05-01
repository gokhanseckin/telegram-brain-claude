"""Canonical prompts — verbatim from docs/mvp-spec.md §5.

`MODEL_VERSION` drives reprocessing: bump it whenever any prompt body
changes. The understanding worker re-runs on messages whose stored
`model_version` differs from this constant.
"""

from tbc_common.prompts.brief import build_brief_system
from tbc_common.prompts.understanding import build_understanding_system
from tbc_common.prompts.weekly import build_weekly_system

MODEL_VERSION = "understanding-2026-05-01-v10-strict-what"

__all__ = [
    "MODEL_VERSION",
    "build_brief_system",
    "build_understanding_system",
    "build_weekly_system",
]
