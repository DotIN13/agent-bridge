from __future__ import annotations

import json


def test_openapi_has_bearer_typed_posts_and_responses(client):
    schema = client.get("/openapi.json").json()
    expected = {
        "/health", "/llms.txt", "/v1/help", "/v1/agents", "/v1/models",
        "/v1/info", "/v1/sessions", "/v1/jobs", "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/events", "/v1/jobs/{job_id}/steer",
        "/v1/jobs/{job_id}/cancel", "/v1/jobs/{job_id}/message",
        "/v1/files", "/v1/files/list", "/v1/files/content"}
    assert expected <= set(schema["paths"])
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    for path in ("/v1/jobs", "/v1/jobs/{job_id}/steer",
                 "/v1/jobs/{job_id}/message", "/v1/files"):
        operation = schema["paths"][path]["post"]
        assert "requestBody" in operation
    submit = schema["paths"]["/v1/jobs"]["post"]
    assert "202" in submit["responses"]
    assert "409" in submit["responses"]

    def assert_refs_resolve(value):
        if isinstance(value, list):
            for item in value:
                assert_refs_resolve(item)
        elif isinstance(value, dict):
            reference = value.get("$ref")
            if reference:
                assert reference.startswith("#/components/schemas/")
                assert reference.rsplit("/", 1)[-1] in \
                    schema["components"]["schemas"]
            for item in value.values():
                assert_refs_resolve(item)

    assert_refs_resolve(schema)
    event_content = schema["paths"]["/v1/jobs/{job_id}/events"]["get"][
        "responses"]["200"]["content"]
    assert {"application/json", "text/event-stream"} <= set(event_content)
    cancel_responses = schema["paths"]["/v1/jobs/{job_id}/cancel"]["post"][
        "responses"]
    assert {"200", "202"} <= set(cancel_responses)
    file_content = schema["paths"]["/v1/files/content"]["get"][
        "responses"]["200"]["content"]
    assert "application/octet-stream" in file_content


def test_auth_and_validation_use_stable_error_envelope(client, auth, gateway):
    unauthorized = client.get("/v1/jobs")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthorized"

    response = client.post("/v1/jobs", headers=auth, json={
        "prompt": "work", "modle": "typo"})
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert gateway.db.list_jobs() == []

    no_session = client.post("/v1/jobs", headers=auth, json={
        "prompt": "work", "fork": False})
    assert no_session.status_code == 400
    assert gateway.db.list_jobs() == []


def test_multipart_submission_rejects_malformed_or_unknown_fields(
        client, auth, gateway):
    cases = [
        {"files": {"payload": ("payload.json", b"{}", "application/json")}},
        {"data": {"payload": "{}", "extra": "x"},
         "files": {"files": ("x.txt", b"x")}},
        {"data": {"payload": "{}"},
         "files": {"other": ("x.txt", b"x")}},
        {"data": {"payload": "{bad"},
         "files": {"files": ("x.txt", b"x")}},
        {"files": {"files": ("x.txt", b"x")}},
    ]
    for kwargs in cases:
        response = client.post("/v1/jobs", headers=auth, **kwargs)
        assert response.status_code == 400
        assert response.json()["error"]["code"] in {
            "invalid_json", "invalid_multipart", "validation_error"}
    assert gateway.db.list_jobs() == []


def test_submit_detail_summary_types_and_internal_fields(client, auth, gateway):
    response = client.post("/v1/jobs", headers=auth, json={
        "prompt": "# Typed work", "include_thinking": True})
    assert response.status_code == 202
    accepted = response.json()
    assert response.headers["location"].endswith(accepted["id"])
    assert gateway.pool.submitted == [accepted["id"]]

    detail = client.get(f"/v1/jobs/{accepted['id']}", headers=auth).json()
    assert detail["fork"] is True
    assert detail["include_thinking"] is True
    assert detail["files"] == []
    assert "title_norm" not in detail

    listing = client.get("/v1/jobs", headers=auth).json()
    assert listing["jobs"][0]["id"] == accepted["id"]
    assert "prompt" not in listing["jobs"][0]
    assert "result" not in listing["jobs"][0]


def test_job_creation_idempotency_replays_and_conflicts(client, auth, gateway):
    headers = {**auth, "Idempotency-Key": "job-one"}
    first = client.post("/v1/jobs", headers=headers, json={"prompt": "once"})
    second = client.post("/v1/jobs", headers=headers, json={"prompt": "once"})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["replayed"] is True
    assert len(gateway.db.list_jobs()) == 1
    assert gateway.pool.submitted == [first.json()["id"]]

    conflict = client.post("/v1/jobs", headers=headers, json={"prompt": "twice"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_refs_ambiguity_and_idempotent_cancel(client, auth, gateway):
    ids = [client.post("/v1/jobs", headers=auth,
            json={"prompt": "same", "title": "Same title"}).json()["id"]
           for _ in range(2)]
    ambiguous = client.get("/v1/jobs/same-title", headers=auth)
    assert ambiguous.status_code == 409
    assert ambiguous.json()["error"]["code"] == "ambiguous_reference"

    first = client.post(f"/v1/jobs/{ids[0]}/cancel", headers=auth)
    assert first.status_code == 202
    gateway.db.finish_job(ids[0], status="canceled", error="canceled")
    again = client.post(f"/v1/jobs/{ids[0]}/cancel", headers=auth)
    assert again.status_code == 200
    assert again.json()["already_terminal"] is True


def test_http_report_id_is_deduplicated(client, auth):
    job = client.post("/v1/jobs", headers=auth,
                      json={"prompt": "report"}).json()
    payload = {"status": "running", "report_id": "scheduler-1"}
    first = client.post(f"/v1/jobs/{job['id']}/message",
                        headers=auth, json=payload)
    second = client.post(f"/v1/jobs/{job['id']}/message",
                         headers=auth, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["seq"] == second.json()["seq"]
    assert second.json()["duplicate"] is True


def test_bounds_and_agent_capabilities(client, auth):
    assert client.get("/v1/jobs?limit=0", headers=auth).status_code == 422
    assert client.get("/v1/jobs?limit=201", headers=auth).status_code == 422
    agents = client.get("/v1/agents", headers=auth).json()
    assert agents["configured"] == ["claude"]
    assert agents["agents"][0]["models"] == ["claude-test"]
    assert agents["agents"][0]["capabilities"]["steering"] is True
    assert agents["features"]["files"] is True


def test_live_agent_help_tracks_typed_contract_and_version(client):
    health = client.get("/health").json()
    help_text = client.get("/llms.txt").text
    assert f"agent-bridge {health['version']}" in help_text
    assert "Idempotency-Key" in help_text
    assert "fork: default true" in help_text
    assert "next_cursor/has_more" in help_text
    assert "monotonic per-job sequence" in help_text
    assert "post-terminal annotations" in help_text
    assert "Strict exactly-once steering is not promised" in help_text
