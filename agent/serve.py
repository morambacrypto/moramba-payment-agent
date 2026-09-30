"""Runs the FastAPI service (`agent.api:app`) on whichever port is free,
instead of failing outright when the usual port (8000) is already taken
by another process — `moramba-payment-agent-serve` (console script) or
`python -m agent.serve`.

Set `PORT` in the environment to pin a specific port instead of
auto-picking one (still fails loudly if that exact port is taken, rather
than silently choosing another — an explicit port request should mean
that port).
"""

import os
import socket

import uvicorn

_DEFAULT_HOST = "127.0.0.1"


def find_free_port(host: str = _DEFAULT_HOST) -> int:
    """Binds to port 0 and lets the OS hand back a free ephemeral port —
    the standard trick, not a scan-and-guess loop that could race another
    process between checking and binding. There's still a tiny window
    between this socket closing and uvicorn binding the same port number
    where another process could grab it first; fine for local/single-
    partner use, not a hardened multi-tenant port broker."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main() -> None:
    host = os.environ.get("HOST", _DEFAULT_HOST)
    pinned_port = os.environ.get("PORT")
    port = int(pinned_port) if pinned_port else find_free_port(host)

    print(f"Starting moramba-payment-agent on http://{host}:{port}")
    if not pinned_port:
        print("(auto-selected a free port — set PORT to pin a specific one instead)")

    uvicorn.run("agent.api:app", host=host, port=port)


if __name__ == "__main__":
    main()
