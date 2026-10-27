"""Fixtures for the end-to-end tests: the real command-line program and the real server process."""

from __future__ import annotations

import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx
import pytest


@pytest.fixture(autouse=True)
def _schema(database: None) -> None:
    """Every test in this folder starts from the loaded schema (the module fixture below runs the scenario once)."""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Server:
    process: subprocess.Popen[str]
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start_server(env: dict[str, str]) -> Server:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "payments_reconciliation.interfaces.serve"],
        env={**env, "API_HOST": "127.0.0.1", "API_PORT": str(port), "LOG_LEVEL": "INFO"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the server exited early:\n{process.stdout.read() if process.stdout else ''}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return Server(process, port)
        except httpx.HTTPError:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError("the server did not become healthy in 60 seconds")


def stop_server(server: Server) -> tuple[int, str]:
    server.process.send_signal(signal.SIGTERM)
    try:
        output, _ = server.process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        server.process.kill()
        output, _ = server.process.communicate()
    return server.process.returncode, output


@pytest.fixture(scope="module")
def scenario(
    tmp_path_factory: pytest.TempPathFactory, database: None
) -> tuple[dict[str, str], subprocess.CompletedProcess[str]]:
    """Run the whole lesson scenario once (the `demo` program) into a temporary settlement folder."""
    folder: pathlib.Path = tmp_path_factory.mktemp("settlement")
    env = {**os.environ, "SETTLEMENT_DIR": str(folder)}
    completed = subprocess.run(
        [sys.executable, "-m", "payments_reconciliation.interfaces.cli", "demo"],
        env=env,
        capture_output=True,
        text=True,
        timeout=3000,
        check=False,
    )
    return env, completed
