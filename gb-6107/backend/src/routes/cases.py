from fastapi import APIRouter, Depends, Header

from src.config.resources import engine
from src.controllers.access import identity, operator
from src.controllers.workbench import new_case, new_run
from src.schemas.cases import CaseEntry, CaseInput
from src.schemas.runs import RunEntry, RunInput, SourceControlInput
from src.services import cases, runs as run_service

router = APIRouter(prefix="/cases", tags=["Cases"])


@router.get("", response_model=list[CaseEntry], dependencies=[Depends(identity)])
def list_cases():
    with engine.connect() as db:
        return cases.list_entries(db)


@router.get("/{case_id}", response_model=CaseEntry, dependencies=[Depends(identity)])
def case_detail(case_id: int):
    with engine.connect() as db:
        return cases.get(db, case_id)


@router.post("", response_model=CaseEntry, status_code=201)
def register(form: CaseInput, user=Depends(operator)):
    return new_case(form, user)


@router.post("/{case_id}/runs", response_model=RunEntry, status_code=202)
def schedule(case_id: int, form: RunInput, user=Depends(operator),
             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    key = idempotency_key or form.idempotency_key
    return new_run(case_id, form, user, key)


@router.put("/{case_id}/reading-sources/control", dependencies=[Depends(operator)])
@router.post("/{case_id}/reading-sources/control", dependencies=[Depends(operator)])
@router.post("/{case_id}/failover", dependencies=[Depends(operator)])
def control_case_sources(case_id: int, form: SourceControlInput, user=Depends(operator)):
    from time import time
    with engine.begin() as db:
        cases.get(db, case_id)
        dumped = form.model_dump(exclude_unset=True)
        config = run_service._source_config_from_input(4, dumped)
        row = run_service.upsert_control(db, "case", config, user["username"], case_id=case_id)
        return dict(row)
