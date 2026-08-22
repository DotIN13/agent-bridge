"""Operator notes: the half of `/v1/info` that no probe can produce."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway.notes import NotesStore
from gateway.server import create_app


class StubCluster:
    """Stands in for the probes, which are not what these tests are about."""

    def __init__(self) -> None:
        self.refreshed = 0
        self.started = False

    def start_async(self) -> None:
        self.started = True

    def get(self) -> dict:
        return {"ready": True, "summary": "host: login5", "gpu": "none"}

    def refresh_async(self) -> None:
        self.refreshed += 1


@pytest.fixture
def probing(gateway):
    gateway.cluster = StubCluster()
    with TestClient(create_app(gateway)) as test_client:
        yield test_client


def test_info_carries_the_notes_beside_the_probes(gateway, probing, auth):
    """One request answers both questions, which is the point of putting them
    together: nobody asks for local conventions they do not know exist."""
    open(gateway.cfg.notes_path, "w", encoding="utf-8").write(
        "## Slurm\n\nUse --account=pi-jevans --partition=jevans-gpu.\n")

    body = probing.get("/v1/info", headers=auth).json()

    assert body["summary"] == "host: login5"
    assert "pi-jevans" in body["notes"]["text"]
    assert body["notes"]["updated_at"]
    # The path, so an agent told to update the notes knows what to open.
    assert body["notes"]["path"] == gateway.cfg.notes_path


def test_absent_notes_are_empty_rather_than_missing(probing, auth):
    """A gateway whose owner has written nothing is not a broken gateway."""
    body = probing.get("/v1/info", headers=auth).json()
    assert body["notes"]["text"] == ""
    assert body["notes"]["updated_at"] is None
    assert body["ready"] is True


def test_unreadable_notes_do_not_break_info(gateway, probing, auth):
    """A directory where the file should be, or a permission problem, must not
    take down the endpoint that answers "what is this machine"."""
    import os

    os.mkdir(gateway.cfg.notes_path)  # the one shape guaranteed to be unreadable
    body = probing.get("/v1/info", headers=auth).json()
    assert body["notes"]["text"] == ""
    assert body["ready"] is True


def test_notes_are_read_fresh_every_time(gateway, probing, auth):
    """Edited over ssh, in place, by an agent — never through this process. A
    cached answer would be a way to serve yesterday's conventions."""
    open(gateway.cfg.notes_path, "w", encoding="utf-8").write("first")
    assert "first" in probing.get("/v1/info", headers=auth).json()["notes"]["text"]

    open(gateway.cfg.notes_path, "w", encoding="utf-8").write("second")
    assert "second" in probing.get("/v1/info", headers=auth).json()["notes"]["text"]


def test_the_served_document_is_capped(gateway, probing, auth):
    """The file is edited out of band, so nothing on the write side bounds it."""
    store = NotesStore(gateway.cfg.notes_path, max_bytes=64)
    open(gateway.cfg.notes_path, "w", encoding="utf-8").write("x" * 500)
    assert len(store.read().text) == 64
