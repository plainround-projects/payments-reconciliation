"""Settlement files on disk, one per business day: `settlement_YYYY-MM-DD.csv`."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from payments_reconciliation.domain.errors import SettlementMissingError


class DirectorySettlementFiles:
    """`SettlementFiles` over a directory. Writes go to a temporary file and are renamed (no half files)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path_for(self, business_date: date) -> Path:
        """Where the file for a day lives."""
        return self._directory / f"settlement_{business_date.isoformat()}.csv"

    def read(self, business_date: date) -> str:
        """The file's text, or `SettlementMissingError` when the provider's file has not arrived."""
        path = self.path_for(business_date)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise SettlementMissingError(f"no settlement file for {business_date.isoformat()} at {path}") from error

    def write(self, business_date: date, text: str) -> None:
        """Store the file for a day, replacing any earlier one."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(business_date)
        temporary = path.with_suffix(".csv.part")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
