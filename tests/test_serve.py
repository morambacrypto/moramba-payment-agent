import socket
from unittest.mock import patch

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


def test_main_auto_selects_a_free_port_when_none_pinned(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    with patch.object(serve, "find_free_port", return_value=54321) as mock_find, \
         patch.object(serve.uvicorn, "run") as mock_run:
        serve.main()

    mock_find.assert_called_once()
    mock_run.assert_called_once_with("agent.api:app", host="127.0.0.1", port=54321)


def test_main_uses_pinned_port_without_calling_find_free_port(monkeypatch):
    monkeypatch.setenv("PORT", "9999")
    with patch.object(serve, "find_free_port") as mock_find, \
         patch.object(serve.uvicorn, "run") as mock_run:
        serve.main()

    mock_find.assert_not_called()
    mock_run.assert_called_once_with("agent.api:app", host="127.0.0.1", port=9999)


def test_main_respects_host_env_var(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8080")
    with patch.object(serve.uvicorn, "run") as mock_run:
        serve.main()

    mock_run.assert_called_once_with("agent.api:app", host="0.0.0.0", port=8080)
