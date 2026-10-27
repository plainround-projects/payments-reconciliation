"""The real clock and the real id source."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID


class SystemClock:
    """The current UTC time."""

    def now(self) -> datetime:
        """Now, timezone-aware."""
        return datetime.now(UTC)


class RandomIds:
    """Random version-4 UUIDs."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        return uuid.uuid4()
