"""目录机会状态与请求成本分开，仍使用同端点事务锁和CAS。"""

from sqlalchemy import text

from app.modules.reporting.store_models import ReportDispatchCursor
from app.modules.sync_control.dispatch_budget import EndpointBudgets


def test_policy_only_commit_does_not_create_or_replace_cost_balance(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    endpoint = "GET /open_api/v1.3/ad/get/"
    with factory() as db, db.begin():
        budget = EndpointBudgets(db)
        assert budget.acquire(seed.tenant_id, endpoint) is not None
        budget.directory_policy(seed.tenant_id, endpoint)["turn"] = 1
        budget.persist()
    with factory() as db:
        assert (
            endpoint not in db.get(ReportDispatchCursor, seed.tenant_id).fairness_state
        )
        assert EndpointBudgets(db).peek_directory_policy(seed.tenant_id, endpoint) == {
            "turn": 1
        }


def test_policy_shadow_is_visible_in_same_batch_and_rolls_back(committed_sync_seed):
    seed, factory = committed_sync_seed
    endpoint = "GET /open_api/v1.3/ad/get/"
    with factory() as db:
        budget = EndpointBudgets(db)
        budget.acquire(seed.tenant_id, endpoint)
        budget.directory_policy(seed.tenant_id, endpoint)["turn"] = 2
        assert budget.peek_directory_policy(seed.tenant_id, endpoint) == {"turn": 2}
        budget.persist()
        db.rollback()
    with factory() as db:
        assert EndpointBudgets(db).peek_directory_policy(seed.tenant_id, endpoint) == {}


def test_independent_endpoint_policies_merge_without_early_tenant_lock(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    first, second = "GET /open_api/v1.3/ad/get/", "GET /open_api/v1.3/campaign/get/"
    with factory() as a, a.begin():
        left = EndpointBudgets(a)
        left.acquire(seed.tenant_id, first)
        left.directory_policy(seed.tenant_id, first)["turn"] = 1
        with factory() as b, b.begin():
            b.execute(text("SET LOCAL lock_timeout='100ms'"))
            right = EndpointBudgets(b)
            assert right.acquire(seed.tenant_id, second) is not None
            right.directory_policy(seed.tenant_id, second)["turn"] = 2
            right.persist()
        left.persist()
    with factory() as db:
        budget = EndpointBudgets(db)
        assert budget.peek_directory_policy(seed.tenant_id, first) == {"turn": 1}
        assert budget.peek_directory_policy(seed.tenant_id, second) == {"turn": 2}


def test_same_endpoint_cannot_bypass_policy_lock(committed_sync_seed):
    seed, factory = committed_sync_seed
    endpoint = "GET /open_api/v1.3/ad/get/"
    with factory() as a, a.begin(), factory() as b, b.begin():
        assert EndpointBudgets(a).acquire(seed.tenant_id, endpoint) is not None
        assert EndpointBudgets(b).acquire(seed.tenant_id, endpoint) is None
