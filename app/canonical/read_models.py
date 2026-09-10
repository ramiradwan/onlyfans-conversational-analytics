"""Neutral canonical read-model value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AccountReadModel:
    view_revision: int = 0
    conversations: dict[str, dict[str, Any]] = field(default_factory=dict)
