from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from client import abclient


def test_sse_parser_handles_comments_and_multiline_data():
    payload = '{"seq":1,"type":"assistant",' + '\n' + '"data":{"text":"ok"}}'
    lines = [b": ping\n", b"id: 1\n", b"event: assistant\n",
             f"data: {payload.splitlines()[0]}\n".encode(),
             f"data: {payload.splitlines()[1]}\n".encode(), b"\n"]
    events = list(abclient.parse_sse(lines))
    assert events == [{"seq": 1, "type": "assistant", "data": {"text": "ok"}}]


def test_sse_parser_accepts_final_frame_without_blank_line():
    events = list(abclient.parse_sse([
        "id: 2\n", "event: status\n",
        'data: {"seq":2,"type":"status","data":{"stage":"done"}}\n']))
    assert events[0]["seq"] == 2


def test_client_iter_events_uses_sse_and_filters_duplicates(monkeypatch):
    class Headers:
        def get(self, key, default=None):
            return "text/event-stream" if key == "Content-Type" else default
    class Response:
        headers = Headers()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def __iter__(self):
            return iter([
                b'id: 1\n', b'event: assistant\n',
                b'data: {"seq":1,"type":"assistant","data":{"text":"old"}}\n', b'\n',
                b'id: 2\n', b'event: assistant\n',
                b'data: {"seq":2,"type":"assistant","data":{"text":"new"}}\n', b'\n'])
    monkeypatch.setattr(abclient.urllib.request, "urlopen",
                        lambda *args, **kwargs: Response())
    client = abclient.Client("test", "http://example", "token")
    monkeypatch.setattr(client, "get_job",
                        lambda _job: {"id": "job", "status": "succeeded"})
    assert [event["seq"] for event in client.iter_events("job", after=1)] == [2]


def test_clean_sse_eof_reconnects_from_cursor(monkeypatch):
    class Headers:
        def get(self, key, default=None):
            return "text/event-stream" if key == "Content-Type" else default
    class Response:
        headers = Headers()
        def __init__(self, seq, terminal=False):
            self.seq = seq
            self.terminal = terminal
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def __iter__(self):
            return iter([
                f"id: {self.seq}\n".encode(), b"event: assistant\n",
                (f'data: {{"seq":{self.seq},"type":"assistant",'
                 f'"data":{{"text":"{self.seq}"}}}}\n').encode(), b"\n"])

    requests = []
    responses = iter([Response(1), Response(2, terminal=True)])
    monkeypatch.setattr(abclient.urllib.request, "urlopen",
                        lambda req, **_kwargs: (requests.append(req), next(responses))[1])
    client = abclient.Client("test", "http://example", "token")
    states = iter([{"status": "running"}, {"status": "succeeded"}])
    monkeypatch.setattr(client, "get_job", lambda _job: next(states))
    assert [event["seq"] for event in client.iter_events("job")] == [1, 2]
    assert requests[1].get_header("Last-event-id") == "1"


def test_sse_timeout_exhaustion_is_not_false_terminal(monkeypatch):
    client = abclient.Client("test", "http://example", "token")
    monkeypatch.setattr(
        abclient.urllib.request, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(abclient.socket.timeout()))
    monkeypatch.setattr(client, "get_job",
                        lambda _job: {"id": "j", "status": "running"})
    with pytest.raises(abclient.GatewayError, match="still running"):
        list(client.iter_events("j", reconnects=1))


def test_wait_socket_timeout_at_deadline_is_normal_timeout(monkeypatch):
    client = abclient.Client("test", "http://example", "token")
    monkeypatch.setattr(
        abclient.urllib.request, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(abclient.socket.timeout()))
    monkeypatch.setattr(client, "get_job",
                        lambda _job: {"id": "j", "status": "running"})
    moments = iter([0.0, 0.0, 1.0, 1.0])
    monkeypatch.setattr(abclient.time, "monotonic", lambda: next(moments))
    result = client.wait("j", timeout=1.0)
    assert result == {"id": "j", "status": "running",
                      "timed_out_waiting": True}


def test_wait_timeout_keeps_remote_job_running(monkeypatch):
    client = abclient.Client("test", "http://example", "token")
    monkeypatch.setattr(client, "get_job",
                        lambda _job: {"id": "j", "status": "running"})
    monkeypatch.setattr(client, "cancel",
                        lambda _job: (_ for _ in ()).throw(AssertionError("must not cancel")))
    moments = iter([0.0, 2.0])
    monkeypatch.setattr(abclient.time, "monotonic", lambda: next(moments))
    result = client.wait("j", timeout=1.0)
    assert result == {"id": "j", "status": "running", "timed_out_waiting": True}


def test_multipart_spools_with_bounded_reads(tmp_path, monkeypatch):
    local = tmp_path / "large.bin"
    local.write_bytes(b"abc")
    real_open = builtins.open
    sizes = []

    class CheckedFile:
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): self.raw.close()
        def read(self, size=None):
            assert size is not None and size > 0
            sizes.append(size)
            return self.raw.read(size)

    def checked_open(path, mode="r", *args, **kwargs):
        raw = real_open(path, mode, *args, **kwargs)
        return CheckedFile(raw) if str(path) == str(local) and "b" in mode else raw

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{}'

    sent = {}
    def urlopen(req, **_kwargs):
        sent["body"] = b"".join(req.data)
        sent["length"] = req.get_header("Content-length")
        return Response()

    monkeypatch.setattr(builtins, "open", checked_open)
    monkeypatch.setattr(abclient.urllib.request, "urlopen", urlopen)
    code, _data = abclient.http_multipart(
        "http://example", "/v1/files", "token", {}, [("large.bin", str(local))])
    assert code == 200
    assert sizes and all(size == 1 << 20 for size in sizes)
    assert int(sent["length"]) == len(sent["body"])
    assert b"abc" in sent["body"]


def test_multipart_filename_rejects_header_injection():
    with pytest.raises(abclient.GatewayError, match="unsafe upload"):
        abclient._multipart_filename('x\r\nInjected: yes')
    assert abclient._multipart_filename('a"b.txt') == 'a\\"b.txt'


def test_upload_preflight_rejects_duplicate_names(tmp_path):
    first = tmp_path / "a" / "same.txt"
    second = tmp_path / "b" / "same.txt"
    first.parent.mkdir(); second.parent.mkdir()
    first.write_text("a"); second.write_text("b")
    with pytest.raises(abclient.GatewayError, match="duplicate upload"):
        abclient._collect_local([str(first), str(second)], None)


def test_upload_preflight_rejects_symlink(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("x")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(abclient.GatewayError, match="non-symlink"):
        abclient._collect_local([str(link)], None)


def test_download_plan_preserves_directory_structure(tmp_path):
    plan = abclient._download_plan(
        ["/remote/out/a/result.csv", "/remote/out/b/result.csv"],
        str(tmp_path), source_dir="/remote/out", flatten=False)
    assert [path.relative_to(tmp_path.resolve()).as_posix() for _remote, path in plan] == [
        "a/result.csv", "b/result.csv"]


def test_flatten_collision_is_rejected(tmp_path):
    with pytest.raises(abclient.GatewayError, match="collision"):
        abclient._download_plan(
            ["/remote/a/result.csv", "/remote/b/result.csv"],
            str(tmp_path), flatten=True)


def test_download_refuses_overwrite_before_network(tmp_path, monkeypatch):
    destination = tmp_path / "result.csv"
    destination.write_text("existing")
    client = abclient.Client("test", "http://example", "token")
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    monkeypatch.setattr(abclient, "http_download", fail_if_called)
    with pytest.raises(abclient.GatewayError, match="overwrite"):
        client.download_files(str(tmp_path), paths=["/remote/result.csv"])
    assert called is False


def test_local_download_failure_is_not_reported_as_network(tmp_path, monkeypatch):
    class EmptyResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, _size): return b""
    monkeypatch.setattr(abclient.urllib.request, "urlopen",
                        lambda *args, **kwargs: EmptyResponse())
    monkeypatch.setattr(abclient.os, "fsync",
                        lambda _fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(abclient.GatewayError, match="flush local") as exc:
        abclient.http_download("http://example", "token", "/x",
                               str(tmp_path / "x"))
    assert "cannot reach" not in str(exc.value)


def test_atomic_download_cleans_partial_on_failure(tmp_path, monkeypatch):
    class BrokenResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, _size): raise OSError("broken")

    monkeypatch.setattr(abclient.urllib.request, "urlopen",
                        lambda *args, **kwargs: BrokenResponse())
    with pytest.raises(abclient.GatewayError):
        abclient.http_download("http://example", "token", "/x",
                               str(tmp_path / "x"))
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / "x").exists()
