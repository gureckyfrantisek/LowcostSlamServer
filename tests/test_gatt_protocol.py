import pytest

from gatt.protocol import messages
from gatt.protocol.commands import COMMANDS, build_request
from gatt.protocol.framing import FramingError, Reassembler, encode
from gatt.protocol.messages import ProtocolError


def test_framing_roundtrip_multi_packet():
    payload = bytes(range(256)) * 3
    frames = encode(payload, 100)
    assert frames[0][0] == 0x01 and frames[-1][0] == 0x02 and frames[1][0] == 0x00
    assert all(len(f) <= 101 for f in frames)

    reassembler = Reassembler()
    results = [reassembler.feed(f) for f in frames]
    assert results[:-1] == [None] * (len(frames) - 1)
    assert results[-1] == payload


def test_framing_single_packet_and_empty():
    assert encode(b"abc", 100) == [b"\x03abc"]
    assert Reassembler().feed(encode(b"", 100)[0]) == b""


def test_framing_errors():
    reassembler = Reassembler()
    with pytest.raises(FramingError):
        reassembler.feed(b"\x00abc")          # continuation without start
    with pytest.raises(FramingError):
        reassembler.feed(b"\x40abc")          # unknown flag
    with pytest.raises(FramingError):
        reassembler.feed(b"")
    small = Reassembler(max_size=4)
    with pytest.raises(FramingError):
        small.feed(b"\x01" + b"x" * 5)


def test_framing_first_packet_restarts_message():
    reassembler = Reassembler()
    assert reassembler.feed(b"\x01old") is None
    assert reassembler.feed(b"\x03new") == b"new"


def test_parse_request():
    request = messages.parse_request(b'{"id":7,"cmd":"projects.list"}')
    assert (request.id, request.cmd, request.params) == (7, "projects.list", {})

    for bad in (b"\xff", b"[]", b'{"cmd":"x"}', b'{"id":true,"cmd":"x"}', b'{"id":1}', b'{"id":1,"cmd":""}'):
        with pytest.raises(ProtocolError) as e:
            messages.parse_request(bad)
        assert e.value.code == messages.MALFORMED_REQUEST


def test_build_request_paths_and_query():
    assert build_request(COMMANDS["projects.list"], {}) == ("/projects", {})
    assert build_request(COMMANDS["projects.files"], {"project_name": "run 1"}) == ("/projects/run%201", {})
    assert build_request(COMMANDS["measurement.start"], {"project_name": "p"}) == ("/start", {"project_name": "p"})
    assert build_request(COMMANDS["camera.delete_file"], {"name": "/DCIM/a b.insv"}) == ("/camera/files//DCIM/a%20b.insv", {})
    assert build_request(COMMANDS["camera.delay_test"], {"count": 3}) == ("/camera/delay_test", {"count": "3"})


def test_cleanup_false_is_never_sent():
    # FastAPI reads the query value as a string, "false" would be truthy and delete data
    _, query = build_request(COMMANDS["projects.download"], {"project_name": "p", "cleanup": False})
    assert query == {"project_name": "p"}
    _, query = build_request(COMMANDS["projects.download"], {"project_name": "p", "cleanup": True})
    assert query == {"project_name": "p", "cleanup": "true"}


@pytest.mark.parametrize("command,params", [
    ("projects.files", {"project_name": ".."}),
    ("projects.files", {"project_name": "a/b"}),
    ("projects.delete", {"project_name": ""}),
    ("projects.delete", {}),
    ("measurement.start", {"project_name": "p", "extra": 1}),
    ("camera.delete_file", {"name": "all"}),
    ("camera.delete_file", {"name": "/x/../y"}),
    ("camera.delay_test", {"count": 0}),
    ("camera.delay_test", {"count": "5"}),
    ("camera.delay_test", {"count": True}),
    ("projects.download", {"project_name": "p", "cleanup": "false"}),
    ("camera.battery", []),
])
def test_invalid_params_rejected(command, params):
    with pytest.raises(ProtocolError) as e:
        build_request(COMMANDS[command], params)
    assert e.value.code == messages.INVALID_PARAMS
