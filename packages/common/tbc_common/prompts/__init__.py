"""Canonical prompts — verbatim from docs/mvp-spec.md §5.

`MODEL_VERSION` drives reprocessing: bump it whenever any prompt body
changes. The understanding worker re-runs on messages whose stored
`model_version` differs from this constant.
"""

from tbc_common.prompts.brief import BRIEF_SYSTEM
from tbc_common.prompts.understanding import UNDERSTANDING_SYSTEM
from tbc_common.prompts.weekly import WEEKLY_SYSTEM

MODEL_VERSION = "understanding-2026-04-22-v1"

__all__ = ["BRIEF_SYSTEM", "MODEL_VERSION", "UNDERSTANDING_SYSTEM", "WEEKLY_SYSTEM"]
