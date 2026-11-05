from __future__ import annotations

import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ssh_socket.py"


def test_binary_proxy_preserves_stream_and_half_close():
    payload = bytes(range(256)) * 4096
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)

        def echo():
            with listener.accept()[0] as connection:
                connection.settimeout(15)
                while chunk := connection.recv(32768):
                    connection.sendall(chunk)
                connection.sendall(b"end-of-stream")

        server = threading.Thread(target=echo, daemon=True)
        server.start()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "127.0.0.1", str(listener.getsockname()[1])],
            input=payload,
            capture_output=True,
            timeout=15,
        )
        server.join(timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == payload + b"end-of-stream"
    assert not server.is_alive()


@pytest.mark.parametrize(
    "arguments",
    [["0"], ["65536"], ["22", "--max-segment", "0"], ["22", "--max-segment", "9000"]],
)
def test_invalid_port_or_segment_size_rejected_without_connecting(arguments):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "invalid.example", *arguments],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "valid port" in result.stderr
