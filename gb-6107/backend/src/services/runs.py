from time import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.constants.job_state import JobState
from src.domain.reading_failover import build_snapshot, normalize_source
from src.domain.scenario import CONFIG
from src.models.cases import cases
from src.models.receipts import receipts
from src.models.runs import runs
from src.models.source_controls import reading_source_controls
from src.services import audit, cases as case_service


def query():
    return (
        select(runs, cases.c.title.label("case_title"), receipts.c.id.label("receipt_id"))
        .join(cases, cases.c.id == runs.c.case_id)
        .outerjoin(receipts, receipts.c.run_id == runs.c.id)
    )


def get(db, run_id):
    run = db.execute(query().where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    return dict(run)


def list_entries(db):
    return [dict(row) for row in db.execute(query().order_by(runs.c.id.desc()).limit(100)).mappings()]


def _source_config_from_input(parameter, data=None, extra=None):
    config = {}
    if data is not None:
        dumped = data.model_dump(exclude_none=True) if hasattr(data, "model_dump") else dict(data)
        for key in (
            "source", "reading_source", "preferred_source", "primary_online", "primary_down",
            "standby_online", "missing_pages", "standby_missing_pages", "backup_missing_pages",
            "replica_missing_pages", "interrupt_after", "fail_after", "crash_after",
            "simulate_interrupt", "options", "primary_disconnect_after",
            "disconnect_after", "down_after_page", "page_payloads", "arrived_pages",
        ):
            if key in dumped:
                config[key] = dumped[key]
    if extra:
        config.update(extra)
    # 统一缺页字段，避免服务层识别多个别名。
    missing = (config.get("standby_missing_pages") or config.get("backup_missing_pages")
               or config.get("replica_missing_pages") or config.get("missing_pages"))
    if missing is not None:
        config["standby_missing_pages"] = missing
    interrupt = config.get("interrupt_after", config.get("fail_after", config.get("crash_after")))
    if interrupt is not None:
        config["interrupt_after"] = int(interrupt)
    if config.get("primary_down") is True:
        config["primary_online"] = False
    if config.get("source"):
        config["preferred_source"] = normalize_source(config["source"])
    elif config.get("reading_source"):
        config["preferred_source"] = normalize_source(config["reading_source"])
    config["parameter"] = parameter
    return config


def _declared_source_config(payload):
    """从资料 JSON 中读取可选的主备演练声明；正常资料没有这些键也不受影响。"""
    if not isinstance(payload, dict):
        return {}
    declared = {}
    for key in ("failover", "source_control", "reading_source_control", "source_config"):
        if isinstance(payload.get(key), dict):
            declared.update(payload[key])
    sources = payload.get("reading_sources") or payload.get("sources")
    if isinstance(sources, dict):
        for canonical, aliases in (
            ("primary", ("primary", "main", "master", "primary_db", "primary_database")),
            ("standby", ("standby", "backup", "replica", "secondary", "standby_db", "backup_db")),
        ):
            side = next((sources[name] for name in aliases if isinstance(sources.get(name), dict)), None)
            if not side:
                continue
            if canonical == "primary" and (
                side.get("online") is False or side.get("down") or side.get("disconnected")
            ):
                declared["primary_online"] = False
            if canonical == "standby":
                missing = side.get("missing_pages") or side.get("missing_indexes")
                if missing is not None:
                    declared["standby_missing_pages"] = missing
                if side.get("online") is False:
                    declared["standby_online"] = False
    return declared


def enqueue(db, case_id, parameter=None, actor="operator", data=None, idempotency_key=None):
    case = case_service.get(db, case_id)
    default_parameter = int(case.get("page_size") or CONFIG["default_parameter"])
    if parameter is None and data is not None:
        parameter = data.parameter or data.page_size or default_parameter
    parameter = int(parameter or default_parameter)
    if parameter > CONFIG["max_parameter"] or parameter < 1:
        raise HTTPException(422, f"参数必须在 1 至 {CONFIG['max_parameter']} 之间")

    key = idempotency_key or (getattr(data, "idempotency_key", None) if data is not None else None)
    if key:
        existing = db.execute(
            select(runs.c.id).where(runs.c.actor == actor, runs.c.idempotency_key == key)
        ).scalar()
        if existing:
            return get(db, existing)

    try:
        snapshot = build_snapshot(case["payload"], parameter)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    source_config = _declared_source_config(case["payload"])
    request_config = _source_config_from_input(parameter, data)
    source_config.update({key: value for key, value in request_config.items() if value is not None})
    source_config["parameter"] = parameter
    now = int(time())
    values = {
        "case_id": case_id,
        "status": JobState.QUEUED,
        "parameter": parameter,
        "actor": actor,
        "created_at": now,
        "checkpoint": 0,
        "reading_snapshot": snapshot,
        "source_config": source_config,
        "idempotency_key": key,
    }
    try:
        change = db.execute(insert(runs).values(**values))
    except IntegrityError:
        if not key:
            raise
        existing = db.execute(
            select(runs.c.id).where(runs.c.actor == actor, runs.c.idempotency_key == key)
        ).scalar_one()
        return get(db, existing)
    run_id = change.inserted_primary_key[0]
    audit.record(db, "enqueue", actor, "run", run_id)
    return get(db, run_id)


def get_receipt(db, run_id):
    row = db.execute(select(receipts).where(receipts.c.run_id == run_id)).mappings().first()
    return dict(row) if row else None


def list_receipts(db, limit=100):
    return [dict(row) for row in db.execute(
        select(receipts).order_by(receipts.c.id.desc()).limit(limit)
    ).mappings()]


def get_control(db, run_id):
    row = db.execute(
        select(reading_source_controls).where(
            reading_source_controls.c.scope == "run",
            reading_source_controls.c.run_id == run_id,
        )
    ).mappings().first()
    return dict(row) if row else None


def control_for_run(db, run):
    """run 级 > case 级 > global > 入队 source_config。"""
    config = dict(run.get("source_config") or {})
    for scope, conditions in (
        ("global", reading_source_controls.c.scope == "global"),
        ("case", (reading_source_controls.c.scope == "case") &
                 (reading_source_controls.c.case_id == run["case_id"])),
        ("run", (reading_source_controls.c.scope == "run") &
                (reading_source_controls.c.run_id == run["id"])),
    ):
        row = db.execute(
            select(reading_source_controls.c.config).where(conditions)
        ).scalar_one_or_none()
        if row:
            config.update(row)
    return config


def upsert_control(db, scope, config, actor, run_id=None, case_id=None):
    now = int(time())
    statement = select(reading_source_controls).where(reading_source_controls.c.scope == scope)
    if run_id is None:
        statement = statement.where(reading_source_controls.c.run_id.is_(None))
    else:
        statement = statement.where(reading_source_controls.c.run_id == run_id)
    if case_id is None:
        statement = statement.where(reading_source_controls.c.case_id.is_(None))
    else:
        statement = statement.where(reading_source_controls.c.case_id == case_id)
    existing = db.execute(statement).mappings().first()
    if existing:
        merged = dict(existing["config"] or {})
        merged.update(config)
        db.execute(reading_source_controls.update().where(
            reading_source_controls.c.id == existing["id"]
        ).values(config=merged, updated_by=actor, updated_at=now))
        control_id = existing["id"]
    else:
        control_id = db.execute(insert(reading_source_controls).values(
            scope=scope, run_id=run_id, case_id=case_id, config=config,
            updated_by=actor, updated_at=now,
        )).inserted_primary_key[0]
    audit.record(db, "source_control", actor, scope, control_id)
    return db.execute(select(reading_source_controls).where(
        reading_source_controls.c.id == control_id
    )).mappings().one()


def save_run_control(db, run_id, data, actor):
    run = get(db, run_id)
    # exclude_unset 保留客户端显式传入的 null，用于清除 interrupt_after/缺页等故障项。
    dumped = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else dict(data)
    if dumped.get("recover"):
        config = {
            "primary_online": True,
            "primary_down": False,
            "standby_online": True,
            "standby_missing_pages": [],
            "interrupt_after": None,
            "recovered_at": int(time()),
        }
    else:
        config = _source_config_from_input(run["parameter"], dumped)
        if config.get("online") is False:
            config["standby_online"] = False
    row = upsert_control(db, "run", config, actor, run_id=run_id, case_id=run["case_id"])
    # 缺页补齐/故障恢复后，把留队单重新放回可领取队列；已完成回执绝不动。
    if run["status"] in (JobState.QUEUED, JobState.WAITING, JobState.PROCESSING, JobState.FAILED):
        values = {"status": JobState.QUEUED, "wait_reason": None}
        if dumped.get("recover") and run["status"] == JobState.FAILED:
            values["failure"] = None
        db.execute(runs.update().where(runs.c.id == run_id).values(**values))
        audit.record(db, "requeue", actor, "run", run_id)
    return dict(row)


def arrive_pages(db, run_id, pages, actor="operator", page_payloads=None):
    """通知必读页已到备库；从缺页集合移除并唤醒留队单。"""
    from src.domain.reading_failover import normalize_page_numbers
    run = get(db, run_id)
    arrived = set(normalize_page_numbers(pages))
    config = control_for_run(db, run)
    current = set(normalize_page_numbers(config.get("standby_missing_pages") or
                                         config.get("missing_pages") or []))
    remaining = sorted(current - arrived)
    update_config = {"standby_missing_pages": remaining, "missing_pages": remaining}
    if page_payloads:
        stored = dict(config.get("page_payloads") or {})
        for page in arrived:
            if str(page) in page_payloads:
                stored[str(page)] = page_payloads[str(page)]
            elif page in page_payloads:
                stored[str(page)] = page_payloads[page]
        update_config["page_payloads"] = stored
    row = upsert_control(db, "run", update_config, actor, run_id=run_id, case_id=run["case_id"])
    db.execute(runs.update().where(runs.c.id == run_id).values(
        status=JobState.QUEUED, wait_reason=None,
    ))
    audit.record(db, "pages_arrived", actor, "run", run_id)
    return dict(row)


def retry(db, run_id, actor="worker"):
    """重试入口幂等：已完成直接返回（含原回执），其他状态从检查点续跑。"""
    run = get(db, run_id)
    receipt = get_receipt(db, run_id)
    if run["status"] == JobState.DONE or receipt:
        return run
    db.execute(runs.update().where(runs.c.id == run_id).values(
        status=JobState.QUEUED,
        failure=None,
        wait_reason=None,
        claim_token=None,
    ))
    audit.record(db, "retry", actor, "run", run_id)
    return get(db, run_id)


def new_claim_token():
    return uuid4().hex
