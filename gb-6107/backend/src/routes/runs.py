from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select

from src.config.resources import cache, engine
from src.constants.job_state import JobState
from src.controllers.access import identity, operator, reviewer
from src.controllers.workbench import new_review, new_run
from src.schemas.reviews import ReviewEntry, ReviewInput
from src.schemas.runs import ReceiptEntry, RunEntry, RunInput, SourceControlInput
from src.services import reviews, runs
from src.services import execution
from src.utils.serial import canonical_json

router = APIRouter(prefix="/runs", tags=["Execution"])
receipt_router = APIRouter(tags=["Receipts"])


@router.post("/process-next", dependencies=[Depends(operator)])
def process_next_run():
    return {"processed": execution.service_one()}


@receipt_router.post("/worker/tick", dependencies=[Depends(operator)])
def worker_tick():
    return {"processed": execution.service_one()}


@router.get("", response_model=list[RunEntry], dependencies=[Depends(identity)])
def list_runs():
    with engine.connect() as db:
        return runs.list_entries(db)


@router.get("/{run_id}", response_model=RunEntry, dependencies=[Depends(identity)])
def run_detail(run_id: int):
    with engine.connect() as db:
        return runs.get(db, run_id)


@router.get("/{run_id}/review", response_model=ReviewEntry | None, dependencies=[Depends(identity)])
def review_detail(run_id: int):
    with engine.connect() as db:
        runs.get(db, run_id)
        return reviews.get(db, run_id)


@router.post("/{run_id}/review", response_model=ReviewEntry, status_code=201)
def sign(run_id: int, form: ReviewInput, user=Depends(reviewer)):
    return new_review(run_id, form, user)


@router.get("/{run_id}/receipt", response_model=ReceiptEntry | None, dependencies=[Depends(identity)])
def receipt_detail(run_id: int):
    with engine.connect() as db:
        runs.get(db, run_id)
        return runs.get_receipt(db, run_id)


@router.post("/{run_id}/receipt", response_model=ReceiptEntry, dependencies=[Depends(operator)])
def issue_or_replay_receipt(run_id: int):
    with engine.begin() as db:
        run = runs.get(db, run_id)
        if run["status"] != JobState.DONE or not run.get("result"):
            raise HTTPException(409, "批次成功后才能取得回执")
        receipt = runs.get_receipt(db, run_id)
    if receipt is None:
        # 兼容历史 done 行没有 receipts 记录；finish 内部仍按 run_id 幂等。
        execution.finish_receipt(run_id, run["result"])
    with engine.connect() as db:
        receipt = runs.get_receipt(db, run_id)
        if receipt is None:
            raise HTTPException(500, "回执保存失败")
        return receipt


@router.get("/{run_id}/receipts", response_model=list[ReceiptEntry], dependencies=[Depends(identity)])
def run_receipts(run_id: int):
    with engine.connect() as db:
        runs.get(db, run_id)
        receipt = runs.get_receipt(db, run_id)
        return [receipt] if receipt else []


@router.post("/{run_id}/retry", response_model=RunEntry, dependencies=[Depends(operator)])
@router.post("/{run_id}/resume", response_model=RunEntry, dependencies=[Depends(operator)])
def retry_run(run_id: int):
    with engine.begin() as db:
        run = runs.retry(db, run_id, "operator")
    try:
        cache.lpush("workbench:wake", str(run_id))
    except Exception:
        pass
    return run


@router.post("/{run_id}/source-control", response_model=RunEntry, dependencies=[Depends(operator)])
@router.put("/{run_id}/source-control", response_model=RunEntry, dependencies=[Depends(operator)])
@router.post("/{run_id}/failover", response_model=RunEntry, dependencies=[Depends(operator)])
@router.post("/{run_id}/reading-source", response_model=RunEntry, dependencies=[Depends(operator)])
def control_run_sources(run_id: int, form: SourceControlInput, user=Depends(operator)):
    with engine.begin() as db:
        runs.save_run_control(db, run_id, form, user["username"])
        run = runs.get(db, run_id)
    try:
        cache.lpush("workbench:wake", str(run_id))
    except Exception:
        pass
    if form.run_now:
        try:
            execution.advance(run_id)
        except Exception as exc:
            # 同步演练时把可诊断信息返回；持久队列/worker 仍是正式执行路径。
            with engine.begin() as db:
                failure = execution.JobFailure(run_id, exc)
                execution.fail(failure)
        with engine.connect() as db:
            run = runs.get(db, run_id)
    return run


@router.post("/{run_id}/advance", response_model=RunEntry, dependencies=[Depends(operator)])
def advance_now(run_id: int):
    with engine.begin() as db:
        runs.retry(db, run_id, "operator")
    try:
        execution.advance(run_id)
    except execution.JobFailure as exc:
        execution.fail(exc)
    with engine.connect() as db:
        return runs.get(db, run_id)


def _control_alias(run_id: int, form: SourceControlInput, user):
    return control_run_sources(run_id, form, user)


@router.post("/{run_id}/pages/arrive", response_model=RunEntry, dependencies=[Depends(operator)])
@router.post("/{run_id}/pages", response_model=RunEntry, dependencies=[Depends(operator)])
def pages_arrived(run_id: int, body: dict | None = None, user=Depends(operator)):
    body = body or {}
    pages = body.get("pages") or body.get("page") or body.get("arrived_pages") or []
    with engine.begin() as db:
        runs.arrive_pages(db, run_id, pages, user["username"], body.get("page_payloads"))
        run = runs.get(db, run_id)
    if body.get("run_now"):
        execution.advance(run_id)
        with engine.connect() as db:
            run = runs.get(db, run_id)
    return run


@router.post("/{run_id}/primary-down", response_model=RunEntry, dependencies=[Depends(operator)])
def primary_down_alias(run_id: int, user=Depends(operator)):
    return control_run_sources(run_id, SourceControlInput(primary_down=True, run_now=True), user)


@router.post("/{run_id}/recover", response_model=RunEntry, dependencies=[Depends(operator)])
def recover_alias(run_id: int, user=Depends(operator)):
    return control_run_sources(run_id, SourceControlInput(recover=True, run_now=True), user)


@router.post("/{run_id}/interrupt", response_model=RunEntry, dependencies=[Depends(operator)])
def interrupt_alias(run_id: int, form: SourceControlInput | None = None, user=Depends(operator)):
    payload = form or SourceControlInput(interrupt_after=1, run_now=True)
    if payload.interrupt_after is None:
        payload.interrupt_after = 1
    payload.run_now = True
    return control_run_sources(run_id, payload, user)


@router.get("/{run_id}/export", dependencies=[Depends(identity)])
def export(run_id: int):
    with engine.connect() as db:
        run = runs.get(db, run_id)
        if run["status"] != JobState.DONE:
            raise HTTPException(409, "没有可导出的执行结果")
        run["review"] = reviews.get(db, run_id)
        receipt = runs.get_receipt(db, run_id)
        if receipt:
            run["receipt_record"] = {key: receipt[key] for key in ("id", "digest", "created_at", "source", "delivery_count")}
    return Response(canonical_json(run), media_type="application/json", headers={
        "Content-Disposition": f"attachment; filename=evidence-{run_id}.json",
    })


@receipt_router.get("/receipts", response_model=list[ReceiptEntry], dependencies=[Depends(identity)])
def all_receipts():
    with engine.connect() as db:
        return runs.list_receipts(db)


@receipt_router.post("/receipts", response_model=ReceiptEntry, dependencies=[Depends(operator)])
def create_or_replay_receipt(body: dict):
    run_id = body.get("run_id") or body.get("runId") or body.get("id")
    if run_id is None:
        raise HTTPException(422, "需要 run_id")
    return issue_or_replay_receipt(int(run_id))


@receipt_router.get("/receipts/{receipt_id}", response_model=ReceiptEntry, dependencies=[Depends(identity)])
def receipt_by_id(receipt_id: int):
    from src.models.receipts import receipts as receipt_table
    with engine.connect() as db:
        row = db.execute(select(receipt_table).where(receipt_table.c.id == receipt_id)).mappings().first()
        if row is None:
            from fastapi import HTTPException
            raise HTTPException(404, "回执不存在")
        return dict(row)


@receipt_router.post("/reading-pages/arrive", response_model=RunEntry, dependencies=[Depends(operator)])
def global_pages_arrived(body: dict, user=Depends(operator)):
    run_id = body.get("run_id") or body.get("runId")
    if run_id is None:
        raise HTTPException(422, "需要 run_id")
    return pages_arrived(int(run_id), body, user)


@receipt_router.get("/reading-sources", dependencies=[Depends(identity)])
def reading_source_status():
    with engine.connect() as db:
        entries = runs.list_entries(db)
    return [{
        "run_id": item["id"], "case_id": item["case_id"], "status": item["status"],
        "reading_source": item.get("reading_source") or (item.get("result") or {}).get("reading_source"),
        "wait_reason": item.get("wait_reason"), "missing_pages": item.get("missing_pages"),
        "checkpoint": item.get("checkpoint"),
    } for item in entries[:50]]


@receipt_router.post("/reading-sources/failover", response_model=RunEntry, dependencies=[Depends(operator)])
@receipt_router.post("/failover", response_model=RunEntry, dependencies=[Depends(operator)])
def global_failover(form: SourceControlInput, run_id: int | None = None,
                    user=Depends(operator)):
    if run_id is None:
        # 兼容把 run_id 放在 JSON/查询参数中的调用。
        run_id = (form.model_extra or {}).get("run_id")
    if run_id is None:
        from fastapi import HTTPException
        raise HTTPException(422, "需要 run_id")
    return control_run_sources(run_id, form, user)
