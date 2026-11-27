"""Service-layer aliases for failover/receipt acceptance tests."""
from src.services.execution import (  # noqa: F401
    JobFailure, JobPaused, advance, build_reading_sources, claim_next,
    finish_receipt, service_one,
)
from src.services.runs import (  # noqa: F401
    enqueue, get_receipt, retry, save_run_control,
)


def process_one():
    return service_one()


def run_one():
    return service_one()


def process_run(run_id):
    return advance(run_id)
