"""Stable, opaque IDs for slides and the editable units inside them.

IDs are assigned once when content is created and never reassigned — display
order is the list position, not the ID. This keeps natural-language edits,
previews, and image assets robust across add/remove/reorder.
"""

from __future__ import annotations

import secrets


def new_id(prefix: str) -> str:
    """e.g. new_id('sl') -> 'sl_9f3a1c20'."""
    return f"{prefix}_{secrets.token_hex(4)}"
