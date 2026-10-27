"""The lesson's scenario through the real command-line program and a real server process, failures included."""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import start_server, stop_server

pytestmark = pytest.mark.e2e


def test_the_demo_program_runs_the_whole_scenario_and_exits_zero(scenario):
    _, completed = scenario
    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    for line in (
        "0 breaks found",  # the clean day
        "breaks found: 6",
        "  ptx_0050  timing",
        "  ptx_0112  fee_discrepancy",
        "  ptx_0200  missing_capture",
        "  ptx_0215  duplicate_on_our_side",
        "  ptx_0300  provider_only_refund",
        "  ptx_0400  amount_mismatch",
        "ptx_0112: fee corrected, +0.18 posted, ledger balanced",
        "ptx_0200: inserted from settlement report, postings added, ledger balanced",
        "ptx_0215: duplicate reversed, one capture retained, ledger balanced",
        "ptx_0300: refund posted from settlement report, ledger balanced",
        "ptx_0112: already resolved",
        "Matched at the first pass: 494 transactions",
        "[balances]",
        "cleared carried differences: ptx_0050",
        "ledger sum across every posting: 0.00",
    ):
        assert line in out, line
    assert "FAILED" not in out and "Traceback" not in completed.stderr


def test_the_server_serves_the_report_and_the_worklist_of_the_planted_day(scenario):
    env, completed = scenario
    assert completed.returncode == 0
    server = start_server(env)
    try:
        report = httpx.get(f"{server.url}/v1/days/2026-10-20/report", timeout=30).json()
        assert report["balances"] is True and report["clean_transactions"] == 494
        assert [line["provider_txn_id"] for line in report["worklist"]] == ["ptx_0400"]
        assert [line["provider_txn_id"] for line in report["resolved"]] == ["ptx_0200", "ptx_0215", "ptx_0300"]
        worklist = httpx.get(f"{server.url}/v1/worklist", timeout=30).json()["worklist"]
        assert [item["provider_txn_id"] for item in worklist] == ["ptx_0400"]
        reconcile = httpx.post(f"{server.url}/v1/days/2026-10-20/reconcile", timeout=60).json()
        assert reconcile["run"]["break_count"] == 1 and reconcile["run"]["clean_transactions"] == 498
        assert [item["provider_txn_id"] for item in reconcile["breaks"] if item["status"] == "NEEDS_REVIEW"] == [
            "ptx_0400"
        ]
        assert httpx.get(f"{server.url}/health", timeout=10).json() == {"status": "ok", "checks": {"database": "ok"}}
        assert httpx.post(f"{server.url}/v1/days/2020-01-01/reconcile", timeout=30).status_code == 404
    finally:
        code, output = stop_server(server)
    assert code == 0, output
    assert "shutdown complete" in output and "level=ERROR" not in output
