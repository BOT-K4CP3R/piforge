"""JSON-lines protocol helpers (spec §5.6): malformed / over-long lines, writer lock safety."""

from __future__ import annotations

import io
import threading

from piforge.twin.ipc import MessageWriter, iter_messages


def test_malformed_lines_become_bad_message_warnings():
    data = b'{"op":"a"}\nnot json\n[1,2]\n{"op":"b"}\n'
    msgs = list(iter_messages(io.BytesIO(data)))
    assert [m["op"] for m in msgs] == ["a", "log", "log", "b"]
    for m in msgs[1:3]:
        assert m["stream"] == "protocol" and m["code"] == "TWIN.BAD_MESSAGE" and m["level"] == "warning"


def test_overlong_line_discarded_with_one_warning():
    big = b"x" * 1000                                       # no JSON, far over the limit
    data = b'{"op":"a"}\n' + big + b"\n" + b'{"op":"b"}\n' + b"y" * 50 + b'{"op":"c"}\n'
    msgs = list(iter_messages(io.BytesIO(data), max_line=40))
    ops = [m["op"] for m in msgs]
    assert ops == ["a", "log", "b", "log"]                  # "y…{c}" line is also over-long → dropped
    warn = msgs[1]
    assert warn["code"] == "TWIN.LINE_TOO_LONG" and warn["stream"] == "protocol"
    assert "1001" in warn["text"]                           # bytes discarded (incl. newline)


def test_writer_releases_lock_when_interrupted():
    class Boom(io.RawIOBase):
        def writable(self):
            return True

        def write(self, b):
            raise KeyboardInterrupt

    w = MessageWriter(Boom())                               # type: ignore[arg-type]
    try:
        w.send({"op": "log", "text": "x"})
    except KeyboardInterrupt:
        pass
    assert w._lock.acquire(timeout=0.5), "lock left held after KeyboardInterrupt"
    w._lock.release()


def test_nonblocking_send_returns_false_when_busy():
    w = MessageWriter(io.BytesIO())
    w._lock.acquire()
    try:
        res: list = []
        t = threading.Thread(target=lambda: res.append(w.send({"op": "x"}, blocking=False)))
        t.start()
        t.join(1.0)
        assert res == [False]
    finally:
        w._lock.release()
    assert w.send({"op": "x"}) is True
