import json
import socket
import threading
import time
import pytest
from fleetsign.mpv_ipc import MpvIpc, encode_command, parse_lines

def test_encode_command():
    raw = encode_command(["loadfile", "a.png"], 7)
    assert raw.endswith(b"\n")
    assert json.loads(raw) == {"command": ["loadfile", "a.png"], "request_id": 7}

def test_parse_lines_partial():
    objs, rest = parse_lines(b'{"event":"idle"}\n{"event":"start')
    assert objs == [{"event": "idle"}]
    assert rest == b'{"event":"start'

def test_command_request_response():
    a, b = socket.socketpair()

    def fake_mpv():
        buf = b""
        while b"\n" not in buf:
            buf += b.recv(1024)
        req = json.loads(buf.split(b"\n", 1)[0])
        b.sendall((json.dumps({"error": "success", "data": None,
                               "request_id": req["request_id"]}) + "\n").encode())
    threading.Thread(target=fake_mpv, daemon=True).start()

    ipc = MpvIpc(a)
    resp = ipc.command("loadfile", "a.png")
    assert resp["error"] == "success"
    ipc.close()

def test_get_event():
    a, b = socket.socketpair()
    ipc = MpvIpc(a)
    b.sendall(b'{"event":"end-file"}\n')
    assert ipc.get_event(timeout=2.0) == {"event": "end-file"}
    ipc.close()

def test_command_raises_connectionerror_on_dead_socket():
    a, b = socket.socketpair()
    ipc = MpvIpc(a)
    b.close()
    a.close()
    with pytest.raises(ConnectionError):
        ipc.command("loadfile", "x", timeout=1.0)
    ipc.close()

def test_timeout_does_not_leak_response():
    a, b = socket.socketpair()  # b never replies
    ipc = MpvIpc(a)
    with pytest.raises(TimeoutError):
        ipc.command("get_property", "fullscreen", timeout=0.3)
    assert ipc._responses == {}
    ipc.close()


def test_late_response_after_timeout_is_dropped():
    # A reply that arrives only AFTER its command gave up must be discarded, not
    # stored forever — otherwise every timed-out command leaks one dict entry.
    a, b = socket.socketpair()
    ipc = MpvIpc(a)
    with pytest.raises(TimeoutError):
        ipc.command("get_property", "fullscreen", timeout=0.2)  # request_id 1
    b.sendall((json.dumps({"error": "success", "data": True,
                           "request_id": 1}) + "\n").encode())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and ipc._responses:
        time.sleep(0.01)
    assert ipc._responses == {}
    ipc.close()
