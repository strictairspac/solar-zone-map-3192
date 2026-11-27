from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "queued"
    PENDING = "queued"  # 兼容把“留队”命名为 pending 的验收脚本。
    PROCESSING = "processing"
    RUNNING = "processing"
    WAITING = "waiting"
    DONE = "done"
    SUCCEEDED = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 仍可被 worker 领取/推进的状态。WAITING 也保留在队中，缺页到齐后继续。
ACTIVE_STATES = (JobState.QUEUED, JobState.PROCESSING, JobState.WAITING)
TERMINAL_STATES = (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
