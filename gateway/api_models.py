"""Typed public API contract for agent-bridge.

The database deliberately keeps a few SQLite-friendly representations (integer
booleans and JSON text).  Nothing in this module exposes those storage details:
all HTTP responses pass through these models or the normalisers below.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, computed_field,
                      model_validator)


# Every event type the gateway can emit. Lives here rather than in the client
# because this module is the contract; `abclient.EVENT_TYPES` mirrors it.
EVENT_TYPES: frozenset[str] = frozenset({
    "status", "assistant", "thinking", "tool_use", "tool_result",
    "steer", "result", "error", "log", "message",
})


def iso_local(epoch: float | None) -> str | None:
    """Epoch seconds -> ISO 8601 in this host's local time, offset attached.

    Local rather than UTC because the reader correlating a job against an
    sbatch log or a terminal scrollback is holding a local clock. The offset is
    always present, so the value stays unambiguous even though "local" here
    means the *gateway's* zone, which for a tunnelled client is not their own.

    Epoch floats are kept alongside these: they are cursors and arithmetic
    inputs, and `Last-Event-ID` is derived from them.
    """
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileItem(StrictModel):
    path: str | None = None
    name: str | None = None
    content_b64: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def valid_shape(self):
        if self.path:
            if self.name or self.content_b64 is not None or self.text is not None:
                raise ValueError("a file path reference cannot include inline content")
            return self
        if not self.name:
            raise ValueError("file item needs path, or name with content_b64/text")
        if (self.content_b64 is None) == (self.text is None):
            raise ValueError("inline file needs exactly one of content_b64 or text")
        return self


class JobCreate(StrictModel):
    prompt: str = Field(min_length=1)
    cwd: str | None = None
    agent: str | None = None
    session: str | None = None
    title: str | None = None
    fork: bool = True
    model: str | None = None
    permission_mode: str | None = None
    include_thinking: bool = False
    files: list[FileItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_job(self):
        self.prompt = self.prompt.strip()
        if not self.prompt:
            raise ValueError("prompt is required")
        if not self.fork and not self.session:
            raise ValueError("fork=false requires session")
        return self


class SteerRequest(StrictModel):
    prompt: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def valid_text(self):
        value = (self.prompt or self.text or "").strip()
        if not value:
            raise ValueError("prompt is required")
        self.prompt = value
        self.text = None
        return self


class MessageRequest(BaseModel):
    """Compute-side report. Extra fields are intentionally retained.

    Batch scripts need to attach scheduler-specific facts without a gateway
    release.  The stable fields provide validation and report deduplication;
    extra fields remain part of the stored event.
    """

    model_config = ConfigDict(extra="allow")
    report_id: str | None = Field(default=None, max_length=200)
    status: Literal["queued", "running", "finished", "failed", "unknown"] | None = None
    msg: str | None = Field(default=None, max_length=16000)
    report: str | None = None
    host: str | None = None
    slurm_job_id: str | None = None
    ts: float | None = None


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorInfo


class JobSummary(BaseModel):
    id: str
    status: str
    agent: str
    title: str | None = None
    cwd: str | None = None
    requested_session: str | None = None
    chosen_session: str | None = None
    forked_session: str | None = None
    model: str | None = None
    fork: bool = True
    include_thinking: bool = False
    cost_usd: float | None = None
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    last_event_at: float | None = None

    @computed_field
    @property
    def session(self) -> str | None:
        """The session to pass back as `session` on the next job.

        There are three session columns and nothing previously said which one
        a caller should reuse, so this is the single documented answer. Under
        `direct` dispatch `forked_session` is correct in every case: a fresh run
        reports the id it created, a fork reports the new branch, and an
        in-place resume reports the target it appended to. The other two are
        fallbacks for a row that has not reached its init record yet.
        """
        return self.forked_session or self.chosen_session or self.requested_session

    @computed_field
    @property
    def created_at_iso(self) -> str | None:
        return iso_local(self.created_at)

    @computed_field
    @property
    def started_at_iso(self) -> str | None:
        return iso_local(self.started_at)

    @computed_field
    @property
    def finished_at_iso(self) -> str | None:
        return iso_local(self.finished_at)

    @computed_field
    @property
    def last_event_at_iso(self) -> str | None:
        return iso_local(self.last_event_at)


class JobDetail(JobSummary):
    prompt: str
    permission_mode: str | None = None
    files: list[str] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None


class JobAccepted(BaseModel):
    id: str
    status: str
    agent: str
    cwd: str
    title: str | None = None
    fork: bool
    include_thinking: bool
    files: list[str] = Field(default_factory=list)
    replayed: bool = False
    # The session to reuse, when it is already knowable. A pinned target is
    # echoed straight back so the caller needs no round trip for the whole
    # follow-up and steer path; a fresh or forked run has no id yet -- it first
    # appears in the agent's init record -- so `pending` says to read it off the
    # job row rather than leaving the caller to wonder.
    session: str | None = None
    session_state: Literal["pinned", "pending"] = "pending"


class JobsPage(BaseModel):
    jobs: list[JobSummary]
    next_cursor: str | None = None
    has_more: bool = False


class EventRecord(BaseModel):
    seq: int
    ts: float
    type: str
    data: dict[str, Any]
    job_id: str | None = None
    # Seconds since this job's first event. Set by the events route, which is
    # the only place that knows the run's start; "where in the run did this
    # happen" is the question a reader actually has, and deriving it otherwise
    # means fetching event #1 first.
    elapsed: float | None = None

    @computed_field
    @property
    def ts_iso(self) -> str | None:
        return iso_local(self.ts)

    @computed_field
    @property
    def elapsed_hms(self) -> str | None:
        if self.elapsed is None:
            return None
        total = int(self.elapsed)
        return f"+{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


class EventsPage(BaseModel):
    events: list[EventRecord]
    status: str
    terminal: bool
    next_after: int
    has_more: bool
    # The shape of the whole log, so a caller can place its window without
    # probing forward for the end.
    total: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    # Kept during the client transition; new clients use the top-level fields.
    job: JobDetail | None = None


class CancelResponse(BaseModel):
    id: str
    status: str | None = None
    canceling: bool = False
    was: str | None = None
    already_terminal: bool = False


class SteerResponse(BaseModel):
    id: str
    delivered: bool
    note: str
    replayed: bool = False


class MessageResponse(BaseModel):
    id: str
    seq: int
    duplicate: bool = False


class AgentDescription(BaseModel):
    name: str
    default_model: str | None = None
    models: list[str] = Field(default_factory=list)
    default_cwd: str
    capabilities: dict[str, Any]


class AgentsResponse(BaseModel):
    configured: list[str]
    known: list[str]
    default: str
    agents: list[AgentDescription]
    features: dict[str, Any]


class ModelsResponse(BaseModel):
    agent: str
    models: list[str]
    default: str | None = None


class SessionsResponse(BaseModel):
    sessions: list[dict[str, Any]]


class UploadResponse(BaseModel):
    upload_id: str
    dir: str
    paths: list[str]


class FileRow(BaseModel):
    path: str
    is_dir: bool
    size: int
    mtime: float


class FilesPage(BaseModel):
    dir: str
    files: list[FileRow]
    next_cursor: str | None = None
    has_more: bool = False


ERROR_RESPONSES = {
    400: {"model": ErrorEnvelope},
    401: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    413: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
}
