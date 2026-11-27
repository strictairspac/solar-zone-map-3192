from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from src.constants.job_state import JobState


class RunInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 旧客户端只传 parameter；新客户端可传 page_size。两者都缺时用演示默认页长。
    parameter: int | None = Field(default=None, ge=1, le=256)
    page_size: int | None = Field(default=None, ge=1, le=256)
    idempotency_key: str | None = Field(default=None, max_length=80)
    source: Any | None = None
    reading_source: Any | None = None
    preferred_source: Any | None = None
    primary_online: bool | None = None
    primary_down: bool | None = None
    standby_online: bool | None = None
    missing_pages: list[int] | None = None
    standby_missing_pages: list[int] | None = None
    backup_missing_pages: list[int] | None = None
    replica_missing_pages: list[int] | None = None
    interrupt_after: int | None = Field(default=None, ge=0)
    fail_after: int | None = Field(default=None, ge=0)
    crash_after: int | None = Field(default=None, ge=0)
    simulate_interrupt: bool | int | None = None
    options: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_page_parameter(self):
        if self.parameter is None and self.page_size is None:
            raise ValueError("需要 parameter（或 page_size）指定每页闪测条数")
        return self

    def resolved_parameter(self, default: int = 4) -> int:
        return self.parameter or self.page_size or default


class SourceControlInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    primary_online: bool | None = None
    primary_down: bool | None = None
    standby_online: bool | None = None
    online: bool | None = None
    missing_pages: list[int] | None = None
    standby_missing_pages: list[int] | None = None
    backup_missing_pages: list[int] | None = None
    replica_missing_pages: list[int] | None = None
    interrupt_after: int | None = Field(default=None, ge=0)
    fail_after: int | None = Field(default=None, ge=0)
    crash_after: int | None = Field(default=None, ge=0)
    recover: bool | None = None
    run_now: bool | None = False


class RunEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    case_id: int
    case_title: str | None = None
    status: JobState
    parameter: int
    actor: str
    created_at: int
    finished_at: int | None = None
    result: dict | None = None
    failure: str | None = None
    digest: str | None = None
    reading_snapshot: dict | None = None
    progress: dict | None = None
    source_config: dict | None = None
    source_report: dict | None = None
    checkpoint: int | None = 0
    reading_source: str | None = None
    wait_reason: str | None = None
    missing_pages: list[int] | None = None
    idempotency_key: str | None = None
    receipt_id: int | None = None
    waiting: bool = False
    queue_state: str | None = None
    source: str | None = None
    source_used: str | None = None
    source_label: str | None = None

    @model_validator(mode="after")
    def fill_source_aliases(self):
        result = self.result or {}
        code = (self.reading_source or self.source or self.source_used
                or result.get("reading_source") or result.get("source")
                or result.get("source_used"))
        if code:
            self.reading_source = code
            self.source = code
            self.source_used = code
        self.source_label = (self.source_label or result.get("reading_source_label")
                             or result.get("source_label") or result.get("source_used_label"))
        self.waiting = self.status == JobState.QUEUED and bool(self.wait_reason or self.missing_pages)
        self.queue_state = JobState.WAITING.value if self.waiting else getattr(self.status, "value", str(self.status))
        return self


class ReceiptEntry(BaseModel):
    id: int
    run_id: int
    case_id: int
    source: str | None = None
    created_at: int
    payload: dict
    digest: str
    delivery_count: int | None = 1
