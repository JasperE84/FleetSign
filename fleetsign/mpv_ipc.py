from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import Optional


def encode_command(args: list, request_id: int) -> bytes:
    return (json.dumps({"command": args, "request_id": request_id}) + "\n").encode("utf-8")


def parse_lines(buffer: bytes) -> tuple[list[dict], bytes]:
    objs: list[dict] = []
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            objs.append(json.loads(line.decode("utf-8")))
        except ValueError:
            continue
    return objs, buffer


def connect_unix(socket_path: str, timeout: float = 10.0) -> socket.socket:
    deadline = time.monotonic() + timeout
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(socket_path)
            return s
        except OSError:
            s.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


class MpvIpc:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""
        self._req_id = 0
        self._send_lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._resp_event = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._closed:
            try:
                data = self._sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            self._buf += data
            objs, self._buf = parse_lines(self._buf)
            for obj in objs:
                if "request_id" in obj:
                    self._responses[obj["request_id"]] = obj
                    self._resp_event.set()
                elif "event" in obj:
                    self._events.put(obj)

    def command(self, *args, timeout: float = 5.0) -> dict:
        with self._send_lock:
            self._req_id += 1
            rid = self._req_id
            try:
                self._sock.sendall(encode_command(list(args), rid))
            except OSError as e:
                raise ConnectionError(f"mpv socket write failed: {e}") from e
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if rid in self._responses:
                return self._responses.pop(rid)
            if not self._reader.is_alive():
                self._responses.pop(rid, None)
                raise ConnectionError("mpv ipc reader stopped")
            self._resp_event.wait(0.1)
            self._resp_event.clear()
        self._responses.pop(rid, None)
        raise TimeoutError(f"mpv command timed out: {args}")

    def get_event(self, timeout: Optional[float]) -> Optional[dict]:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
