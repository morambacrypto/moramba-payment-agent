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

Set `SSL=1` to serve over HTTPS instead of plain HTTP — needed because
Claude's web app (unlike Claude Code) only accepts an `https://` URL for
a remote MCP server, even a local one. A self-signed cert for
127.0.0.1/localhost is generated once and cached (_CERT_FILE/_KEY_FILE),
not regenerated on every start, since a browser that already trusted a
previous cert would otherwise need to re-trust a new one each restart.
It's opt-in, not the default, because a self-signed cert is untrusted by
default — the browser/client will warn the first time, and nothing
about this makes 127.0.0.1 reachable from outside this machine.
"""

import ipaddress
import os
import signal
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn

_DEFAULT_HOST = "127.0.0.1"
_PREFERRED_PORT = 58417
_PID_FILE = ".moramba_payment_agent.pid"
_CERT_FILE = ".moramba_payment_agent_cert.pem"
_KEY_FILE = ".moramba_payment_agent_key.pem"
_STOP_TIMEOUT_SECONDS = 5.0


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


def _ensure_self_signed_cert(cert_path: str, key_path: str) -> None:
    """Generates a self-signed TLS cert/key for 127.0.0.1/localhost if
    one doesn't already exist, so SSL=1 needs neither an external
    `openssl` call nor a cert the user has to supply themselves. Cached
    on disk rather than regenerated every start — see the module
    docstring for why that matters."""
    if Path(cert_path).exists() and Path(key_path).exists():
        return

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    Path(key_path).write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(key_path, 0o600)


def main() -> None:
    host = os.environ.get("HOST", _DEFAULT_HOST)
    pinned_port = os.environ.get("PORT")
    ssl_enabled = os.environ.get("SSL", "").lower() in ("1", "true", "yes")

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

    ssl_kwargs = {}
    scheme = "http"
    if ssl_enabled:
        _ensure_self_signed_cert(_CERT_FILE, _KEY_FILE)
        ssl_kwargs = {"ssl_certfile": _CERT_FILE, "ssl_keyfile": _KEY_FILE}
        scheme = "https"

    print(f"Starting moramba-payment-agent on {scheme}://{host}:{port}")
    if ssl_enabled:
        print(
            f"(self-signed cert — your browser/client will warn it's untrusted the first "
            f"time; accept it once, or trust {_CERT_FILE} in your OS/browser store)"
        )
    if not pinned_port:
        print(f"(tried preferred port {_PREFERRED_PORT} first, auto-selected otherwise — set PORT to pin a specific one instead)")

    uvicorn.run("agent.api:app", host=host, port=port, **ssl_kwargs)


if __name__ == "__main__":
    main()
