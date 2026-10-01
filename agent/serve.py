"""Runs the FastAPI service (`agent.api:app`) on whichever port is free,
instead of failing outright when the usual port (8000) is already taken
by another process — `moramba-payment-agent-serve` (console script) or
`python -m agent.serve`.

Set `PORT` in the environment to pin a specific port. Without one,
_PREFERRED_PORT is tried first (chosen from IANA's dynamic/private range
specifically because no well-known software defaults there, so it's
unlikely to collide with anything already running).

Either way, if the target port turns out to be held by a *previous run
of this same tool* (tracked via _PID_FILE, written on every successful
start), that old process is stopped so this one can take the port over
— running the command again is meant to replace the running instance,
not pile up a second one next to it. If the port is held by something
else entirely, it's never touched: the pinned-port case then fails
loudly (an explicit port request should mean that port, not a silent
substitute), and the preferred-port case falls back to an OS-assigned
free port instead.

Claude's web app's MCP connector calls out from Anthropic's own servers,
not the browser, so a `localhost`/`127.0.0.1` URL can never reach it —
"our servers cannot reach your local machine" is Claude's own error for
this, and no amount of local TLS changes that. Set `TUNNEL=1` to expose
this local server through a free Cloudflare quick tunnel
(`cloudflared tunnel --url ...`) — no account, no domain, no signup, a
public `https://*.trycloudflare.com` URL in seconds, with Cloudflare's
own real, trusted certificate (terminated at their edge). The
trade-off: quick-tunnel URLs are random and change every restart, not a
stable address — acceptable for testing from Claude's web app, not a
permanent production endpoint. Requires the `cloudflared` binary on
PATH; prints install instructions and continues serving locally (for
Claude Code, which has no such restriction) if it isn't found.
"""

import os
import re
import signal
import socket
import subprocess
import threading
import time

import uvicorn

_DEFAULT_HOST = "127.0.0.1"
_PREFERRED_PORT = 58417
_PID_FILE = ".moramba_payment_agent.pid"
_STOP_TIMEOUT_SECONDS = 5.0
_TRYCLOUDFLARE_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


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


def _is_port_free(host: str, port: int) -> bool:
    """Same bind-then-close check as find_free_port, but against a
    specific port instead of letting the OS assign one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


def _read_previous_pid() -> int | None:
    try:
        return int(open(_PID_FILE).read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_pid_file() -> None:
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _looks_like_our_process(pid: int) -> bool:
    """Best-effort extra check (Linux only, via /proc) before killing a
    pid found in _PID_FILE — cuts down the (already small) risk of a
    reused pid pointing at some unrelated process that happens to still
    be alive. Not available on macOS/Windows, where this returns True
    and liveness alone governs — same local/single-partner trust level
    already accepted elsewhere in this file."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode(errors="replace")
    except FileNotFoundError:
        return True
    return "moramba" in cmdline.lower()


def _stop_previous_instance(pid: int, host: str, port: int) -> None:
    """Only ever called against a pid this tool itself wrote to
    _PID_FILE on a prior run, confirmed still alive — never an arbitrary
    process merely found holding the port."""
    print(f"Port {port} is held by a previous moramba-payment-agent-serve process (pid {pid}) — stopping it.")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _is_port_free(host, port):
            return
        time.sleep(0.2)


def _resolve_port(host: str, port: int) -> int | None:
    """Returns `port` if it's free, or becomes free after stopping a
    previous run of this same tool holding it; `None` if it's held by
    something else and still is after that attempt."""
    if _is_port_free(host, port):
        return port
    previous_pid = _read_previous_pid()
    if previous_pid is not None and _process_alive(previous_pid) and _looks_like_our_process(previous_pid):
        _stop_previous_instance(previous_pid, host, port)
    return port if _is_port_free(host, port) else None


def _watch_tunnel_output(process: subprocess.Popen) -> None:
    for line in process.stdout:
        match = _TRYCLOUDFLARE_URL_RE.search(line)
        if match:
            print(f"Public URL (Cloudflare quick tunnel, for Claude's web app): {match.group(0)}")
            return


def _start_quick_tunnel(host: str, port: int) -> subprocess.Popen | None:
    """Launches a Cloudflare "quick tunnel" forwarding to this local
    server — see the module docstring for why this is what Claude's web
    app actually needs. Returns `None` without raising if `cloudflared`
    isn't installed, since the local service is still fully usable (for
    Claude Code) either way."""
    try:
        process = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://{host}:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError:
        print(
            "TUNNEL=1 was set, but `cloudflared` isn't installed (or not on PATH) — "
            "serving locally only. Install it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ "
            "and re-run to get a public URL."
        )
        return None

    threading.Thread(target=_watch_tunnel_output, args=(process,), daemon=True).start()
    return process


def main() -> None:
    host = os.environ.get("HOST", _DEFAULT_HOST)
    pinned_port = os.environ.get("PORT")
    tunnel_enabled = os.environ.get("TUNNEL", "").lower() in ("1", "true", "yes")

    if pinned_port:
        requested = int(pinned_port)
        port = _resolve_port(host, requested)
        if port is None:
            raise RuntimeError(f"port {requested} is already in use by another process")
    else:
        port = _resolve_port(host, _PREFERRED_PORT)
        if port is None:
            port = find_free_port(host)

    _write_pid_file()

    print(f"Starting moramba-payment-agent on http://{host}:{port}")
    if not pinned_port:
        print(f"(tried preferred port {_PREFERRED_PORT} first, auto-selected otherwise — set PORT to pin a specific one instead)")

    tunnel_process = _start_quick_tunnel(host, port) if tunnel_enabled else None
    try:
        uvicorn.run("agent.api:app", host=host, port=port)
    finally:
        if tunnel_process is not None:
            tunnel_process.terminate()


if __name__ == "__main__":
    main()
