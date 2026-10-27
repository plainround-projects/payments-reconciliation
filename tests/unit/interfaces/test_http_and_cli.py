"""The HTTP layer through FastAPI's test client and the command-line handlers, over in-memory storage."""

from __future__ import annotations

import argparse
import dataclasses
from datetime import date

import pytest
from fastapi.testclient import TestClient

from payments_reconciliation.application.health import CheckHealth
from payments_reconciliation.domain.errors import InvalidDateError
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants
from payments_reconciliation.interfaces import cli
from payments_reconciliation.interfaces.http.app import create_app
from tests.unit.conftest import World, build_world

DAY = date(2026, 10, 20)
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)


class _Down:
    def check(self) -> None:
        raise ConnectionError("database unreachable")


@pytest.fixture
def client(tmp_path):
    world = build_world(tmp_path)
    world.services.simulator.run(DayPlan(DAY, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    with TestClient(create_app(lambda: world.services), raise_server_exceptions=False) as test_client:
        yield test_client, world


def test_reconcile_over_http_lists_the_six_breaks_with_their_causes(client):
    test_client, _ = client
    body = test_client.post("/v1/days/2026-10-20/reconcile").json()
    assert {item["provider_txn_id"]: item["cause"] for item in body["breaks"]} == {
        "ptx_0005": "timing",
        "ptx_0008": "fee_discrepancy",
        "ptx_0012": "missing_capture",
        "ptx_0015": "duplicate_on_our_side",
        "ptx_0020": "provider_only_refund",
        "ptx_0025": "amount_mismatch",
    }
    assert body["run"]["break_count"] == 6 and body["run"]["clean_transactions"] == 54


def test_resolve_report_and_worklist_over_http(client):
    test_client, _ = client
    test_client.post("/v1/days/2026-10-20/reconcile")
    resolved = test_client.post("/v1/days/2026-10-20/resolve").json()["resolutions"]
    assert {item["provider_txn_id"]: item["outcome"] for item in resolved}["ptx_0008"] == "resolved"
    again = test_client.post("/v1/days/2026-10-20/resolve").json()["resolutions"]
    assert {item["provider_txn_id"]: item["outcome"] for item in again}["ptx_0008"] == "already resolved"
    report = test_client.get("/v1/days/2026-10-20/report").json()
    assert report["balances"] is True and [line["provider_txn_id"] for line in report["worklist"]] == ["ptx_0025"]
    assert "Worklist (needs a human): ptx_0025" in report["text"]
    worklist = test_client.get("/v1/worklist").json()["worklist"]
    assert [item["provider_txn_id"] for item in worklist] == ["ptx_0025"]


def test_errors_have_one_shape_and_the_right_status(client):
    test_client, _ = client
    missing = test_client.post("/v1/days/2026-10-25/reconcile")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "settlement_missing"
    not_run = test_client.get("/v1/days/2026-10-20/report")
    assert not_run.status_code == 404 and not_run.json()["error"]["code"] == "report_not_available"
    bad_date = test_client.post("/v1/days/not-a-date/reconcile")
    assert bad_date.status_code == 400 and bad_date.json()["error"]["code"] == "invalid_request"


def test_a_malformed_settlement_file_is_a_422_that_names_the_line(client):
    test_client, world = client
    world.services.files.write(
        date(2026, 10, 22), "provider_txn_id,amount,fee,status,settled_at\nptx_1,x,0,settled,now\n"
    )
    response = test_client.post("/v1/days/2026-10-22/reconcile")
    assert response.status_code == 422 and "line 2" in response.json()["error"]["message"]


def test_health_live_and_a_down_dependency(client):
    test_client, world = client
    assert test_client.get("/health/live").json() == {"status": "ok"}
    assert test_client.get("/health").json() == {"status": "ok", "checks": {}}
    down = dataclasses.replace(world.services, health=CheckHealth({"database": _Down()}))
    with TestClient(create_app(lambda: down)) as down_client:
        response = down_client.get("/health")
    assert response.status_code == 503 and response.json()["checks"] == {"database": "ConnectionError"}


def test_an_unexpected_error_is_a_generic_500_and_the_container_is_closed_on_shutdown(tmp_path):
    world = build_world(tmp_path)
    closed: list[bool] = []

    class Boom:
        def execute(self) -> None:
            raise RuntimeError("secret internals")

    broken = dataclasses.replace(world.services, worklist=Boom(), close=lambda: closed.append(True))  # type: ignore[arg-type]
    with TestClient(create_app(lambda: broken), raise_server_exceptions=False) as test_client:
        response = test_client.get("/v1/worklist")
        assert response.status_code == 500 and "secret" not in response.text
        assert response.json()["error"]["code"] == "internal_error"
    assert closed == [True]


def test_parse_date_accepts_iso_and_refuses_the_rest():
    assert cli.parse_date("2026-10-20") == DAY
    with pytest.raises(InvalidDateError):
        cli.parse_date("20/10/2026")


def test_the_parser_requires_a_command_and_a_date():
    parser = cli._parser()
    assert parser.parse_args(["reconcile", "--date", "2026-10-20"]).date == DAY
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile"])


def test_the_commands_print_the_lessons_lines(tmp_path, capsys):
    world: World = build_world(tmp_path)
    args = argparse.Namespace(date=DAY, payments=500, seed=43, prefix="ptx_", plant=True)
    assert cli.cmd_simulate(world.services, args) == 0
    assert cli.cmd_reconcile(world.services, args) == 0
    assert cli.cmd_resolve(world.services, args) == 0
    assert cli.cmd_report(world.services, args) == 0
    assert cli.cmd_worklist(world.services, args) == 0
    out = capsys.readouterr().out
    assert "the process died after the provider captured: ptx_0200" in out
    assert "breaks found: 6" in out and "  ptx_0215  duplicate_on_our_side" in out
    assert "ptx_0112: fee corrected, +0.18 posted, ledger balanced" in out
    assert "ptx_0400  amount_mismatch" in out


def test_an_empty_worklist_says_so(tmp_path, capsys):
    world = build_world(tmp_path)
    assert cli.cmd_worklist(world.services, argparse.Namespace()) == 0
    assert "the worklist is empty" in capsys.readouterr().out


def test_the_demo_runs_the_whole_scenario_and_checks_itself(tmp_path, capsys):
    world = build_world(tmp_path)
    assert cli.cmd_demo(world.services, argparse.Namespace(payments=500)) == 0
    out = capsys.readouterr().out
    assert "0 breaks found" in out and "6 breaks found, 6 classified" in out
    assert "ptx_0112: already resolved" in out and "identical" in out
    assert "cleared carried differences: ptx_0050" in out
    assert "ledger sum across every posting: 0.00" in out


def test_expect_raises_the_scenario_error():
    with pytest.raises(cli.ScenarioError, match="boom"):
        cli.expect(False, "boom")
    cli.expect(True, "never shown")
