#!/usr/bin/env python3
"""OpenSSH ProxyCommand with a per-connection TCP segment limit for WSL tunnels."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading


def connect(host: str, port: int, max_segment: int) -> socket.socket:
    last_error = None
    for family, kind, protocol, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        stream = socket.socket(family, kind, protocol)
        try:
            stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, max_segment)
            stream.settimeout(10)
            stream.connect(address)
            stream.settimeout(None)
            return stream
        except OSError as exc:
            last_error = exc
            stream.close()
    raise last_error or OSError("The SSH host has no usable address.")


def relay(stream: socket.socket) -> None:
    def send():
        try:
            while chunk := os.read(sys.stdin.fileno(), 32768):
                stream.sendall(chunk)
            stream.shutdown(socket.SHUT_WR)
        except OSError:
            stream.close()

    threading.Thread(target=send, daemon=True).start()
    try:
        while chunk := stream.recv(32768):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    finally:
        stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--max-segment", type=int, default=1200)
    args = parser.parse_args()
    if not 536 <= args.max_segment <= 1400 or not 1 <= args.port <= 65535:
        raise SystemExit("Use a valid port and a TCP segment size from 536 to 1400.")
    try:
        relay(connect(args.host, args.port, args.max_segment))
    except OSError as exc:
        raise SystemExit(f"SSH transport ended ({type(exc).__name__}).") from None


if __name__ == "__main__":
    main()
