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
permanent production endpoint.

No manual install step: if `cloudflared` isn't already on PATH, the
binary for this platform/architecture is downloaded once from
Cloudflare's own official GitHub releases and cached under
`~/.cache/moramba-payment-agent/` (not re-fetched on later runs) —
`pip install` plus `TUNNEL=1 moramba-payment-agent-serve` is the whole
setup. Only falls back to printing manual install instructions (and
still serves locally — Claude Code has no such restriction) when
there's no prebuilt release for this platform/arch, or the download
itself fails (e.g. no network).
"""

import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

_DEFAULT_HOST = "127.0.0.1"
_PREFERRED_PORT = 58417
_PID_FILE = ".moramba_payment_agent.pid"
_STOP_TIMEOUT_SECONDS = 5.0
_TRYCLOUDFLARE_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
_CLOUDFLARED_CACHE_DIR = Path.home() / ".cache" / "moramba-payment-agent"
_CLOUDFLARED_RELEASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"


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


def _cloudflared_release_asset() -> tuple[str, bool] | None:
    """Returns (asset filename, is_tarball) for this platform/arch, as
    named in cloudflared's GitHub releases — or `None` if there's no
    prebuilt release for it. Linux/Windows assets are raw executables;
    macOS ships as a .tgz containing one."""
    system = platform.system()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        return None

    if system == "Linux":
        return f"cloudflared-linux-{arch}", False
    if system == "Darwin":
        return f"cloudflared-darwin-{arch}.tgz", True
    if system == "Windows" and arch == "amd64":
        return "cloudflared-windows-amd64.exe", False
    return None


def _download_cloudflared() -> str | None:
    """Downloads cloudflared for this platform from its official GitHub
    releases into _CLOUDFLARED_CACHE_DIR, so TUNNEL=1 needs no manual
    install step. Cached after the first download, not re-fetched on
    later runs. Returns the cached executable's path, or `None` if this
    platform/arch has no prebuilt release or the download fails (e.g. no
    network) — callers fall back to printing manual install instructions
    either way, never to failing the whole command."""
    asset = _cloudflared_release_asset()
    if asset is None:
        return None
    asset_name, is_tarball = asset

    exe_name = "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"
    cached_path = _CLOUDFLARED_CACHE_DIR / exe_name
    if cached_path.exists():
        return str(cached_path)

    print(f"TUNNEL=1: downloading cloudflared for this platform (one-time, cached under {_CLOUDFLARED_CACHE_DIR})...")
    try:
        _CLOUDFLARED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            downloaded_path = Path(tmp_dir) / asset_name
            urllib.request.urlretrieve(_CLOUDFLARED_RELEASE_URL.format(asset=asset_name), downloaded_path)
            if is_tarball:
                with tarfile.open(downloaded_path) as tar:
                    tar.extractall(tmp_dir, filter="data")
                (Path(tmp_dir) / "cloudflared").rename(cached_path)
            else:
                downloaded_path.rename(cached_path)
    except Exception as exc:  # noqa: BLE001 - any download/extract failure falls back to manual install
        print(f"TUNNEL=1: couldn't download cloudflared automatically ({exc}).")
        return None

    if platform.system() != "Windows":
        cached_path.chmod(cached_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(cached_path)


def _resolve_cloudflared_path() -> str | None:
    """Prefers an already-installed `cloudflared` on PATH over the
    auto-downloaded/cached one, so a partner who already has it (or
    wants a specific version) isn't overridden."""
    return shutil.which("cloudflared") or _download_cloudflared()


def _start_quick_tunnel(host: str, port: int) -> subprocess.Popen | None:
    """Launches a Cloudflare "quick tunnel" forwarding to this local
    server — see the module docstring for why this is what Claude's web
    app actually needs. Returns `None` without raising if cloudflared
    isn't available and couldn't be downloaded, since the local service
    is still fully usable (for Claude Code) either way."""
    cloudflared_path = _resolve_cloudflared_path()
    if cloudflared_path is None:
        print(
            "TUNNEL=1 was set, but cloudflared isn't available for this platform and "
            "couldn't be downloaded automatically — serving locally only. Install it "
            "yourself from https://developers.cloudflare.com/cloudflare-one/connections/"
            "connect-networks/downloads/ and re-run to get a public URL."
        )
        return None

    try:
        process = subprocess.Popen(
            [cloudflared_path, "tunnel", "--url", f"http://{host}:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except OSError as exc:
        print(f"TUNNEL=1: failed to start cloudflared ({exc}) — serving locally only.")
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
