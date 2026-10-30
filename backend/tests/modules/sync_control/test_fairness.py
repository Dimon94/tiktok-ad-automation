from collections import Counter

from app.modules.sync_control.fairness import WEIGHTS, choose_work_class


def test_saturated_classes_preserve_history_directory_and_maintenance_progress():
    state, served = {}, Counter()
    for _ in range(100):
        winner, state = choose_work_class(state, WEIGHTS)
        served[winner] += 1
    assert dict(served) == WEIGHTS


def test_single_class_borrows_idle_capacity_without_accumulating_future_debt():
    state = {}
    for _ in range(100):
        winner, state = choose_work_class(state, ["history"])
        assert winner == "history"
    served = Counter()
    for _ in range(100):
        winner, state = choose_work_class(state, WEIGHTS)
        served[winner] += 1
    assert dict(served) == WEIGHTS
