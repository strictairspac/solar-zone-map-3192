import logging
from time import time

from sqlalchemy import case, select, update

from src.config.resources import engine
from src.constants.job_state import JobState
from src.domain.reading_failover import (
    PRIMARY,
    STANDBY,
    DictReadingSource,
    PageNotArrived,
    ReadingInterrupted,
    RequiredPagesMissing,
    build_receipt,
    build_snapshot,
)
from src.models.cases import cases
from src.models.receipts import receipt, receipts
from src.models.runs import runs
from src.services import runs as run_service
from src.services.audit import record
from src.utils.serial import content_digest

logger = logging.getLogger(__name__)

STALE_CLAIM_SECONDS = 30


class JobFailure(Exception):
    def __init__(self, run_id, cause):
        super().__init__(str(cause))
        self.run_id = run_id
        self.cause = cause


class JobPaused(Exception):
    """可恢复暂停（例如读页中断）；不应把批次写成 failed。"""

    def __init__(self, run_id, reason):
        super().__init__(reason)
        self.run_id = run_id
        self.reason = reason


def _missing_indexes(config):
    missing = (config.get("standby_missing_pages") or config.get("backup_missing_pages")
               or config.get("replica_missing_pages") or config.get("missing_pages") or [])
    indexes = set()
    for page in missing:
        try:
            indexes.add(max(0, int(page) - 1))
        except (TypeError, ValueError):
            continue
    return indexes


def build_reading_sources(run, db):
    """根据持久化故障控制构造主/备读数适配器。

    验收方可 monkeypatch 本函数返回自己的 FakePrimary/FakeStandby。
    """
    config = run_service.control_for_run(db, run)
    snapshot = run["reading_snapshot"]
    preferred = config.get("preferred_source") or config.get("source") or config.get("reading_source")
    if preferred:
        from src.domain.reading_failover import normalize_source
        preferred = normalize_source(preferred)

    primary_online = config.get("primary_online")
    if primary_online is None:
        primary_online = not bool(config.get("primary_down", False))
    if preferred == STANDBY and "primary_online" not in config and not config.get("primary_down"):
        primary_online = False

    standby_online = config.get("standby_online")
    if standby_online is None:
        standby_online = config.get("online", True)

    interrupt_after = config.get("interrupt_after", config.get("fail_after", config.get("crash_after")))
    interrupt_after = int(interrupt_after) if interrupt_after is not None else None
    disconnect_after = config.get(
        "primary_disconnect_after", config.get("disconnect_after", config.get("down_after_page"))
    )
    disconnect_after = int(disconnect_after) if disconnect_after is not None else None
    primary = DictReadingSource.from_snapshot(
        snapshot, PRIMARY, online=bool(primary_online), interrupt_after_reads=interrupt_after,
        disconnect_after_reads=disconnect_after,
    )
    standby = DictReadingSource.from_snapshot(
        snapshot, STANDBY, online=bool(standby_online),
        missing_indexes=_missing_indexes(config),
        interrupt_after_reads=interrupt_after,
    )
    return primary, standby, config


def claim_next():
    """领取最旧的可执行单。

    缺页等待中的单保留 queued 状态和 wait_reason，但不会阻塞后面的单；控制补齐
    页后由 retry/control 清空 wait_reason。processing 超过租约的单可被恢复领取。
    """
    now = int(time())
    with engine.begin() as db:
        statement = (
            select(runs)
            .where(runs.c.status == JobState.QUEUED)
            # 先领从未等待的新单，再回头试探缺页/中断单是否已恢复。
            .order_by(case((runs.c.wait_reason.is_(None), 0), else_=1), runs.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        task = db.execute(statement).mappings().first()
        if task is None:
            stale_before = now - STALE_CLAIM_SECONDS
            task = db.execute(
                select(runs)
                .where(runs.c.status == JobState.PROCESSING, runs.c.claimed_at < stale_before)
                .order_by(runs.c.id).limit(1).with_for_update(skip_locked=True)
            ).mappings().first()
        if task is None:
            return None

        run_id = task["id"]
        values = {
            "status": JobState.PROCESSING,
            "claim_token": run_service.new_claim_token(),
            "claimed_at": now,
        }
        if task["reading_snapshot"] is None:
            payload = db.scalar(select(cases.c.payload).where(cases.c.id == task["case_id"]))
            values["reading_snapshot"] = build_snapshot(payload, task["parameter"])
        db.execute(update(runs).where(runs.c.id == run_id).values(**values))
        return run_id


def _save_progress(run_id, state, **extra):
    values = {
        "progress": state,
        "checkpoint": int(state.get("cursor") or 0),
        "claim_token": None,
        "claimed_at": None,
    }
    values.update(extra)
    with engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == run_id).values(**values))


def _pause(run_id, state, reason, missing_pages=None):
    _save_progress(
        run_id,
        state,
        status=JobState.QUEUED,
        wait_reason=reason[:320],
        missing_pages=missing_pages,
    )


def finish_receipt(run_id, receipt_body):
    digest = content_digest(receipt_body)
    now = int(time())
    with engine.begin() as db:
        current = db.execute(
            select(runs.c).where(runs.c.id == run_id).with_for_update()
        ).mappings().one()
        existing = db.execute(
            select(receipts.c.id).where(receipts.c.run_id == run_id)
        ).scalar_one_or_none()
        if existing is not None or current["status"] == JobState.DONE:
            return run_service.get_receipt(db, run_id)

        source_code = receipt_body.get("reading_source")
        case_id = current["case_id"]
        receipt_id = db.execute(receipts.insert().values(
            run_id=run_id, case_id=case_id, source=source_code, created_at=now,
            payload=receipt_body, digest=digest, delivery_count=1,
        )).inserted_primary_key[0]
        # 单数别名表仅用于兼容按 receipt 命名查找的验收脚本。
        db.execute(receipt.insert().values(
            id=receipt_id, run_id=run_id, case_id=case_id, source=source_code,
            created_at=now, payload=receipt_body, digest=digest,
        ))
        db.execute(update(runs).where(runs.c.id == run_id).values(
            status=JobState.DONE,
            result=receipt_body,
            digest=digest,
            finished_at=now,
            progress=None,
            checkpoint=receipt_body.get("checkpoint", {}).get("page_count", current["parameter"]),
            reading_source=source_code,
            source_report={
                key: receipt_body.get(key)
                for key in (
                    "reading_source", "reading_source_label", "used_sources",
                    "primary_unavailable", "failover_count", "primary", "standby",
                    "standby_missing_skippable_pages", "standby_missing_required_pages",
                )
                if key in receipt_body
            },
            wait_reason=None,
            missing_pages=None,
            claim_token=None,
            claimed_at=None,
            failure=None,
        ))
        record(db, str(JobState.DONE), "worker", "run", run_id)
        return run_service.get_receipt(db, run_id)


def advance(run_id):
    with engine.connect() as db:
        run = run_service.get(db, run_id)
        if run["status"] == JobState.DONE:
            return {"paused": None, "receipt": run_service.get_receipt(db, run_id)}
        primary, standby, _config = build_reading_sources(run, db)

    snapshot = run["reading_snapshot"] or build_snapshot_from_case(run["case_id"], run["parameter"])
    state = run.get("progress") or None

    try:
        from src.domain.reading_failover import advance_one, restore_progress
        state = restore_progress(state)
        while int(state["cursor"]) < int(snapshot["page_count"]):
            state = advance_one(snapshot, state, primary=primary, standby=standby)
            # 每页提交一次检查点：之后进程中断，已扫页和命中不会丢。
            _save_progress(run_id, state)
    except RequiredPagesMissing as exc:
        missing = exc.pages
        waiting = {**state, "waiting_pages": missing}
        reason = f"{exc}；缺页不能算缺测，单据留队等待"
        _pause(run_id, waiting, reason, missing_pages=missing)
        return {"paused": "waiting", "missing_pages": missing, "checkpoint": state["cursor"]}
    except ReadingInterrupted as exc:
        interrupted = {**state, "interrupted": True, "interrupt_reason": str(exc)}
        _pause(run_id, interrupted, str(exc), missing_pages=None)
        return {"paused": "interrupted", "checkpoint": state["cursor"]}

    receipt_body = build_receipt(snapshot, state, primary, standby)
    finish_receipt(run_id, receipt_body)
    return {"paused": None, "receipt": receipt_body}


def build_snapshot_from_case(case_id, parameter):
    with engine.begin() as db:
        payload = db.scalar(select(cases.c.payload).where(cases.c.id == case_id))
        snapshot = build_snapshot(payload, parameter)
        db.execute(update(runs).where(runs.c.case_id == case_id).values(reading_snapshot=snapshot))
        return snapshot


def service_one():
    run_id = claim_next()
    if run_id is None:
        return False
    try:
        outcome = advance(run_id)
    except Exception as exc:
        # Let the failed database transaction roll back before persisting the error.
        raise JobFailure(run_id, exc) from exc
    return not bool(outcome.get("paused"))


class JobFailureCompat(JobFailure):
    pass


def fail(job_failure):
    with engine.begin() as db:
        run_id = job_failure.run_id
        statement = select(runs.c.status).where(runs.c.id == run_id).with_for_update()
        if db.scalar(statement) == JobState.DONE:
            return
        cause = job_failure.cause
        if isinstance(cause, (ValueError, TypeError, KeyError, OverflowError)):
            reason = str(cause) or "输入资料或批次参数不符合要求"
        else:
            logger.exception("Execution %s failed", run_id, exc_info=cause)
            reason = "处理失败，请检查 worker 日志"
        now = int(time())
        db.execute(update(runs).where(runs.c.id == run_id).values(
            status=JobState.FAILED,
            failure=reason[:320],
            finished_at=now,
            claim_token=None,
            claimed_at=None,
        ))
        record(db, str(JobState.FAILED), "worker", "run", run_id)
