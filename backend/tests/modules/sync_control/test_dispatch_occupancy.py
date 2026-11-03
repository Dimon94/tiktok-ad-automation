from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.jobs.admission import admission_keys, admit_call
from app.modules.sync_control.occupancy import occupancy_keys, reserve_dispatch
from tests.modules.sync_control.test_endpoint_readiness import _policy, _scope


def test_successful_nested_reservation_is_released_if_outer_transaction_fails(
    redis_client,
):
    from app.modules.sync_control.occupancy import reservation_transaction

    scope, token = _scope(), uuid4()
    with pytest.raises(RuntimeError, match="outer failure"):
        with reservation_transaction(redis_client):
            with reservation_transaction(redis_client):
                assert (
                    reserve_dispatch(
                        redis_client, reservation_id=token, policy=_policy(), **scope
                    ).capacity_hint
                    > 0
                )
            raise RuntimeError("outer failure")
    assert all(redis_client.zcard(key) == 0 for key in occupancy_keys(**scope))


def test_two_controllers_cannot_reserve_same_free_slot(redis_client):
    scope, policy = _scope(), _policy(endpoint_calls_per_window=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: reserve_dispatch(
                    redis_client, reservation_id=uuid4(), policy=policy, **scope
                ),
                range(2),
            )
        )
    assert sorted(result.capacity_hint for result in results) == [0, 1]
    assert redis_client.zcard(admission_keys(**scope)[1]) == 0
    assert redis_client.zcard(occupancy_keys(**scope)[1]) == 1


def test_send_handoff_removes_queued_occupancy_and_consumes_once(redis_client):
    scope, policy, token = _scope(), _policy(), str(uuid4())
    assert (
        reserve_dispatch(
            redis_client, reservation_id=token, policy=policy, **scope
        ).capacity_hint
        == 10
    )
    assert admit_call(
        redis_client,
        policy=policy,
        lease_id=uuid4(),
        dispatch_reservation_id=token,
        **scope,
    ).granted
    assert all(redis_client.zcard(key) == 0 for key in occupancy_keys(**scope))
    assert all(redis_client.zcard(key) == 1 for key in admission_keys(**scope))


def test_queued_endpoint_does_not_spend_independent_endpoint_budget(redis_client):
    scope, policy = _scope(), _policy(endpoint_calls_per_window=1)
    reserve_dispatch(redis_client, reservation_id=uuid4(), policy=policy, **scope)
    other = scope | {"endpoint": "independent", "rate_group": "independent"}
    assert (
        reserve_dispatch(
            redis_client, reservation_id=uuid4(), policy=policy, **other
        ).capacity_hint
        == 1
    )
