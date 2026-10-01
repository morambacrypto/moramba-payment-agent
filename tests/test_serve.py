import os
import signal
import socket
import tarfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent import serve


def test_find_free_port_returns_a_real_bindable_port():
    port = serve.find_free_port()
    assert 0 < port < 65536
    # The port must actually be free right after — bind it ourselves to prove it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_find_free_port_returns_different_ports_across_calls():
    # Not a hard guarantee (OS could theoretically reuse one), but wildly
    # likely across a handful of calls — catches a hardcoded-port regression.
    ports = {serve.find_free_port() for _ in range(5)}
    assert len(ports) > 1


def test_is_port_free_true_for_an_unbound_port():
    port = serve.find_free_port()
    assert serve._is_port_free("127.0.0.1", port) is True


def test_is_port_free_false_for_a_port_already_bound():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert serve._is_port_free("127.0.0.1", port) is False


def test_read_previous_pid_returns_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert serve._read_previous_pid() is None


def test_read_previous_pid_returns_none_for_garbage_contents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / serve._PID_FILE).write_text("not-a-pid")
    assert serve._read_previous_pid() is None


def test_write_and_read_pid_file_round_trips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    serve._write_pid_file()
    assert serve._read_previous_pid() == os.getpid()


def test_process_alive_true_for_this_process():
    assert serve._process_alive(os.getpid()) is True


def test_process_alive_false_for_a_pid_that_does_not_exist():
    assert serve._process_alive(2**30) is False  # vanishingly unlikely to be a real pid


def test_looks_like_our_process_true_when_proc_is_unavailable():
    with patch("builtins.open", side_effect=FileNotFoundError):
        assert serve._looks_like_our_process(999_999) is True


def test_looks_like_our_process_checks_cmdline_contents():
    with patch("builtins.open", return_value=_FakeCmdlineFile(b"/usr/bin/python3\x00moramba-payment-agent-serve\x00")):
        assert serve._looks_like_our_process(123) is True
    with patch("builtins.open", return_value=_FakeCmdlineFile(b"some-other-program\x00")):
        assert serve._looks_like_our_process(123) is False


class _FakeCmdlineFile:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._data


def test_resolve_port_returns_port_immediately_when_free():
    with patch.object(serve, "_is_port_free", return_value=True):
        assert serve._resolve_port("127.0.0.1", 12345) == 12345


def test_resolve_port_stops_a_previous_instance_and_reclaims_the_port():
    calls = {"n": 0}

    def fake_is_free(host, port):
        calls["n"] += 1
        return calls["n"] > 1  # taken on the first check, free after "stopping"

    with patch.object(serve, "_is_port_free", side_effect=fake_is_free), \
         patch.object(serve, "_read_previous_pid", return_value=4242), \
         patch.object(serve, "_process_alive", return_value=True), \
         patch.object(serve, "_looks_like_our_process", return_value=True), \
         patch.object(serve, "_stop_previous_instance") as mock_stop:
        port = serve._resolve_port("127.0.0.1", 58417)

    mock_stop.assert_called_once_with(4242, "127.0.0.1", 58417)
    assert port == 58417


def test_resolve_port_leaves_an_unrelated_processs_port_alone():
    with patch.object(serve, "_is_port_free", return_value=False), \
         patch.object(serve, "_read_previous_pid", return_value=None):
        assert serve._resolve_port("127.0.0.1", 58417) is None


def test_resolve_port_does_not_kill_a_dead_pids_leftover_file():
    with patch.object(serve, "_is_port_free", return_value=False), \
         patch.object(serve, "_read_previous_pid", return_value=4242), \
         patch.object(serve, "_process_alive", return_value=False), \
         patch.object(serve, "_stop_previous_instance") as mock_stop:
        assert serve._resolve_port("127.0.0.1", 58417) is None
    mock_stop.assert_not_called()


def test_resolve_port_does_not_kill_a_pid_that_does_not_look_like_our_process():
    with patch.object(serve, "_is_port_free", return_value=False), \
         patch.object(serve, "_read_previous_pid", return_value=4242), \
         patch.object(serve, "_process_alive", return_value=True), \
         patch.object(serve, "_looks_like_our_process", return_value=False), \
         patch.object(serve, "_stop_previous_instance") as mock_stop:
        assert serve._resolve_port("127.0.0.1", 58417) is None
    mock_stop.assert_not_called()


def test_stop_previous_instance_sends_sigterm_and_waits_for_the_port(monkeypatch):
    sent = {}

    def fake_kill(pid, sig):
        sent["pid"] = pid
        sent["sig"] = sig

    calls = {"n": 0}

    def fake_is_free(host, port):
        calls["n"] += 1
        return calls["n"] > 2  # free on the third poll

    monkeypatch.setattr(serve.os, "kill", fake_kill)
    monkeypatch.setattr(serve.time, "sleep", lambda s: None)
    with patch.object(serve, "_is_port_free", side_effect=fake_is_free):
        serve._stop_previous_instance(4242, "127.0.0.1", 58417)

    assert sent == {"pid": 4242, "sig": signal.SIGTERM}


def test_stop_previous_instance_gives_up_quietly_if_process_already_gone(monkeypatch):
    def fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(serve.os, "kill", fake_kill)
    serve._stop_previous_instance(4242, "127.0.0.1", 58417)  # must not raise


def test_main_uses_preferred_port_when_resolve_succeeds(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.chdir(tmp_path)
    with patch.object(serve, "_resolve_port", return_value=serve._PREFERRED_PORT) as mock_resolve, \
         patch.object(serve, "find_free_port") as mock_find, \
         patch.object(serve.uvicorn, "run") as mock_run:
        serve.main()

    mock_resolve.assert_called_once_with("127.0.0.1", serve._PREFERRED_PORT)
    mock_find.assert_not_called()
    mock_run.assert_called_once_with("agent.api:app", host="127.0.0.1", port=serve._PREFERRED_PORT)
    assert (tmp_path / serve._PID_FILE).read_text() == str(os.getpid())
    assert f"http://127.0.0.1:{serve._PREFERRED_PORT}/mcp" in capsys.readouterr().out


def test_main_falls_back_to_a_free_port_when_preferred_port_resolve_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.chdir(tmp_path)
    with patch.object(serve, "_resolve_port", return_value=None), \
         patch.object(serve, "find_free_port", return_value=54321) as mock_find, \
         patch.object(serve.uvicorn, "run") as mock_run:
        serve.main()

    mock_find.assert_called_once()
    mock_run.assert_called_once_with("agent.api:app", host="127.0.0.1", port=54321)


def test_main_uses_pinned_port_when_resolve_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.chdir(tmp_path)
    with patch.object(serve, "_resolve_port", return_value=9999) as mock_resolve, \
         patch.object(serve, "find_free_port") as mock_find, \
         patch.object(serve.uvicorn, "run") as mock_run:
        serve.main()

    mock_resolve.assert_called_once_with("127.0.0.1", 9999)
    mock_find.assert_not_called()
    mock_run.assert_called_once_with("agent.api:app", host="127.0.0.1", port=9999)


def test_main_raises_when_pinned_port_resolve_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.chdir(tmp_path)
    with patch.object(serve, "_resolve_port", return_value=None), \
         patch.object(serve.uvicorn, "run") as mock_run:
        raised = None
        try:
            serve.main()
        except RuntimeError as exc:
            raised = exc

    assert raised is not None
    assert "8080" in str(raised)
    mock_run.assert_not_called()


def test_main_respects_host_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.chdir(tmp_path)
    with patch.object(serve, "_resolve_port", return_value=8080), \
         patch.object(serve.uvicorn, "run") as mock_run:
        serve.main()

    mock_run.assert_called_once_with("agent.api:app", host="0.0.0.0", port=8080)


def test_watch_tunnel_output_prints_the_trycloudflare_url(capsys):
    fake_process = MagicMock()
    fake_process.stdout = iter([
        "some startup noise\n",
        "INFO | https://random-words-here.trycloudflare.com\n",
        "this line is never reached\n",
    ])

    url_ready = threading.Event()
    serve._watch_tunnel_output(fake_process, url_ready)

    assert url_ready.is_set()
    assert "https://random-words-here.trycloudflare.com/mcp" in capsys.readouterr().out


def test_watch_tunnel_output_returns_quietly_when_no_url_appears(capsys):
    fake_process = MagicMock()
    fake_process.stdout = iter(["no url in this output\n"])

    url_ready = threading.Event()
    serve._watch_tunnel_output(fake_process, url_ready)  # must not raise

    assert not url_ready.is_set()

    assert "trycloudflare.com" not in capsys.readouterr().out


def test_announce_tunnel_pending_prints_once_uvicorn_is_listening_and_url_not_ready(capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        serve._announce_tunnel_pending("127.0.0.1", port, threading.Event())

    assert "Creating the Cloudflare tunnel" in capsys.readouterr().out


def test_announce_tunnel_pending_stays_quiet_if_the_url_already_printed(capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        already_ready = threading.Event()
        already_ready.set()

        serve._announce_tunnel_pending("127.0.0.1", port, already_ready)

    assert "Creating the Cloudflare tunnel" not in capsys.readouterr().out


def test_announce_tunnel_pending_gives_up_quietly_if_nothing_ever_listens(capsys):
    free_port = serve.find_free_port()  # nothing is listening on it

    serve._announce_tunnel_pending("127.0.0.1", free_port, threading.Event(), wait_seconds=0.3)

    assert "Creating the Cloudflare tunnel" not in capsys.readouterr().out


def test_start_quick_tunnel_returns_none_when_cloudflared_unavailable(capsys):
    with patch.object(serve, "_resolve_cloudflared_path", return_value=None):
        result = serve._start_quick_tunnel("127.0.0.1", 58417)

    assert result is None
    assert "cloudflared" in capsys.readouterr().out.lower()


def test_start_quick_tunnel_returns_none_when_popen_fails(capsys):
    with patch.object(serve, "_resolve_cloudflared_path", return_value="/path/to/cloudflared"), \
         patch.object(serve.subprocess, "Popen", side_effect=OSError("permission denied")):
        result = serve._start_quick_tunnel("127.0.0.1", 58417)

    assert result is None
    assert "cloudflared" in capsys.readouterr().out.lower()


def test_start_quick_tunnel_launches_the_resolved_cloudflared_with_the_right_url():
    fake_process = MagicMock()
    fake_process.stdout = iter([])
    with patch.object(serve, "_resolve_cloudflared_path", return_value="/path/to/cloudflared"), \
         patch.object(serve.subprocess, "Popen", return_value=fake_process) as mock_popen, \
         patch.object(serve.threading, "Thread") as mock_thread:
        result = serve._start_quick_tunnel("127.0.0.1", 58417)

    assert result is fake_process
    mock_popen.assert_called_once_with(
        [
            "/path/to/cloudflared", "tunnel", "--url", "http://127.0.0.1:58417",
            "--http-host-header", "127.0.0.1:58417",
        ],
        stdout=serve.subprocess.PIPE, stderr=serve.subprocess.STDOUT, text=True, bufsize=1,
    )
    # One thread watches cloudflared's output for the public URL, the
    # other prints the "tunnel is being created" note — both daemons, so
    # neither can keep the process alive after uvicorn exits.
    assert mock_thread.call_count == 2
    assert all(call.kwargs.get("daemon") is True for call in mock_thread.call_args_list)


def test_resolve_cloudflared_path_prefers_path_over_download():
    with patch.object(serve.shutil, "which", return_value="/usr/bin/cloudflared"), \
         patch.object(serve, "_download_cloudflared") as mock_download:
        result = serve._resolve_cloudflared_path()

    assert result == "/usr/bin/cloudflared"
    mock_download.assert_not_called()


def test_resolve_cloudflared_path_falls_back_to_download_when_not_on_path():
    with patch.object(serve.shutil, "which", return_value=None), \
         patch.object(serve, "_download_cloudflared", return_value="/cached/cloudflared") as mock_download:
        result = serve._resolve_cloudflared_path()

    assert result == "/cached/cloudflared"
    mock_download.assert_called_once()


def test_cloudflared_release_asset_linux_amd64(monkeypatch):
    monkeypatch.setattr(serve.platform, "system", lambda: "Linux")
    monkeypatch.setattr(serve.platform, "machine", lambda: "x86_64")
    assert serve._cloudflared_release_asset() == ("cloudflared-linux-amd64", False)


def test_cloudflared_release_asset_linux_arm64(monkeypatch):
    monkeypatch.setattr(serve.platform, "system", lambda: "Linux")
    monkeypatch.setattr(serve.platform, "machine", lambda: "aarch64")
    assert serve._cloudflared_release_asset() == ("cloudflared-linux-arm64", False)


def test_cloudflared_release_asset_macos_arm64_is_a_tarball(monkeypatch):
    monkeypatch.setattr(serve.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(serve.platform, "machine", lambda: "arm64")
    assert serve._cloudflared_release_asset() == ("cloudflared-darwin-arm64.tgz", True)


def test_cloudflared_release_asset_windows_amd64(monkeypatch):
    monkeypatch.setattr(serve.platform, "system", lambda: "Windows")
    monkeypatch.setattr(serve.platform, "machine", lambda: "AMD64")
    assert serve._cloudflared_release_asset() == ("cloudflared-windows-amd64.exe", False)


def test_cloudflared_release_asset_none_for_unknown_arch(monkeypatch):
    monkeypatch.setattr(serve.platform, "system", lambda: "Linux")
    monkeypatch.setattr(serve.platform, "machine", lambda: "riscv64")
    assert serve._cloudflared_release_asset() is None


def test_cloudflared_release_asset_none_for_windows_arm(monkeypatch):
    monkeypatch.setattr(serve.platform, "system", lambda: "Windows")
    monkeypatch.setattr(serve.platform, "machine", lambda: "arm64")
    assert serve._cloudflared_release_asset() is None


def test_download_cloudflared_returns_none_for_unsupported_platform(monkeypatch):
    monkeypatch.setattr(serve, "_cloudflared_release_asset", lambda: None)
    assert serve._download_cloudflared() is None


def test_download_cloudflared_returns_cached_path_without_redownloading(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "_CLOUDFLARED_CACHE_DIR", tmp_path)
    monkeypatch.setattr(serve.platform, "system", lambda: "Linux")
    cached = tmp_path / "cloudflared"
    cached.write_bytes(b"already here")

    with patch.object(serve.urllib.request, "urlretrieve") as mock_fetch:
        result = serve._download_cloudflared()

    assert result == str(cached)
    mock_fetch.assert_not_called()


def test_download_cloudflared_fetches_a_raw_binary_for_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "_CLOUDFLARED_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(serve.platform, "system", lambda: "Linux")
    monkeypatch.setattr(serve.platform, "machine", lambda: "x86_64")

    def fake_urlretrieve(url, dest):
        assert url.endswith("cloudflared-linux-amd64")
        Path(dest).write_bytes(b"fake binary contents")

    with patch.object(serve.urllib.request, "urlretrieve", side_effect=fake_urlretrieve):
        result = serve._download_cloudflared()

    assert result == str(tmp_path / "cache" / "cloudflared")
    assert Path(result).read_bytes() == b"fake binary contents"
    assert os.access(result, os.X_OK)


def test_download_cloudflared_extracts_a_tarball_for_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "_CLOUDFLARED_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(serve.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(serve.platform, "machine", lambda: "arm64")

    def fake_urlretrieve(url, dest):
        assert url.endswith("cloudflared-darwin-arm64.tgz")
        inner_dir = Path(dest).parent / "_inner"
        inner_dir.mkdir()
        binary_path = inner_dir / "cloudflared"
        binary_path.write_bytes(b"fake macos binary")
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(binary_path, arcname="cloudflared")

    with patch.object(serve.urllib.request, "urlretrieve", side_effect=fake_urlretrieve):
        result = serve._download_cloudflared()

    assert result == str(tmp_path / "cache" / "cloudflared")
    assert Path(result).read_bytes() == b"fake macos binary"


def test_download_cloudflared_returns_none_and_prints_on_network_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(serve, "_CLOUDFLARED_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(serve.platform, "system", lambda: "Linux")
    monkeypatch.setattr(serve.platform, "machine", lambda: "x86_64")

    with patch.object(serve.urllib.request, "urlretrieve", side_effect=OSError("no network")):
        result = serve._download_cloudflared()

    assert result is None
    assert "couldn't download" in capsys.readouterr().out.lower()


def test_main_does_not_start_a_tunnel_without_tunnel_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("TUNNEL", raising=False)
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.chdir(tmp_path)
    with patch.object(serve, "_resolve_port", return_value=9999), \
         patch.object(serve, "_start_quick_tunnel") as mock_start_tunnel, \
         patch.object(serve.uvicorn, "run"):
        serve.main()

    mock_start_tunnel.assert_not_called()


def test_main_starts_and_terminates_the_tunnel_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNNEL", "1")
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.chdir(tmp_path)
    fake_tunnel_process = MagicMock()
    with patch.object(serve, "_resolve_port", return_value=9999), \
         patch.object(serve, "_start_quick_tunnel", return_value=fake_tunnel_process) as mock_start_tunnel, \
         patch.object(serve.uvicorn, "run"):
        serve.main()

    mock_start_tunnel.assert_called_once_with("127.0.0.1", 9999)
    fake_tunnel_process.terminate.assert_called_once()


def test_main_terminates_the_tunnel_even_if_uvicorn_run_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNNEL", "1")
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.chdir(tmp_path)
    fake_tunnel_process = MagicMock()
    with patch.object(serve, "_resolve_port", return_value=9999), \
         patch.object(serve, "_start_quick_tunnel", return_value=fake_tunnel_process), \
         patch.object(serve.uvicorn, "run", side_effect=RuntimeError("boom")):
        try:
            serve.main()
        except RuntimeError:
            pass

    fake_tunnel_process.terminate.assert_called_once()
