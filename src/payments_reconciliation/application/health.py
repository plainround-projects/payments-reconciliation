"""The health check: is every dependency reachable?"""

from __future__ import annotations

from payments_reconciliation.application.ports import HealthProbe


class CheckHealth:
    """Asks each probe and reports its status."""

    def __init__(self, probes: dict[str, HealthProbe]) -> None:
        self._probes = probes

    def execute(self) -> dict[str, str]:
        """Each dependency's status, `ok` or the error's type name."""
        results: dict[str, str] = {}
        for name, probe in self._probes.items():
            try:
                probe.check()
                results[name] = "ok"
            except Exception as error:  # a health check reports every failure; it must not raise
                results[name] = type(error).__name__
        return results
