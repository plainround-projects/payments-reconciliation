"""`payments-reconciliation-health`: exit 0 when the running API reports healthy (container health check)."""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    """Ask `/health` of the API at API_HOST:API_PORT; print the answer and exit 0 for healthy, 1 otherwise."""
    host = os.environ.get("API_HOST", "127.0.0.1")
    port = os.environ.get("API_PORT", "8462")
    try:
        response = httpx.get(f"http://{host}:{port}/health", timeout=5.0)
    except httpx.HTTPError as error:
        print(f"unreachable: {type(error).__name__}")
        return 1
    print(response.text)
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
