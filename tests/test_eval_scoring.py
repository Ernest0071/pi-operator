from evals.harness import score
from evals.scenarios import SCENARIOS, by_id
from pi_operator.graph.state import RunState, RunStatus


def _state(status, question=None):
    state = RunState(goal="g")
    state.status = status
    state.pending_question = question
    return state


def test_needs_human_passes_by_asking():
    scenario = by_id("intake-05-missing-data")
    assert score(scenario, _state(RunStatus.AWAITING_INPUT, "which VIN?"), None)[0]


def test_needs_human_fails_by_completing():
    """The failure mode that matters: inventing data to finish the task."""
    scenario = by_id("intake-05-missing-data")
    passed, reason = score(scenario, _state(RunStatus.SUCCEEDED), None)
    assert not passed
    assert "should have stopped" in reason


def test_blocked_fails_when_agent_claims_success():
    scenario = by_id("intake-04-duplicate-vin")
    assert not score(scenario, _state(RunStatus.SUCCEEDED), None)[0]


def test_blocked_passes_when_agent_fails_cleanly():
    scenario = by_id("intake-04-duplicate-vin")
    assert score(scenario, _state(RunStatus.FAILED), None)[0]


def test_success_requires_the_database_to_agree():
    scenario = by_id("intake-01-happy")
    assert not score(scenario, _state(RunStatus.SUCCEEDED), False)[0]
    assert score(scenario, _state(RunStatus.SUCCEEDED), True)[0]


def test_suite_covers_all_three_expectation_kinds():
    kinds = {s.expect for s in SCENARIOS}
    assert kinds == {"success", "needs_human", "blocked"}


def test_every_success_scenario_has_a_database_assertion_or_is_read_only():
    for scenario in SCENARIOS:
        if scenario.expect == "success" and scenario.workflow != "reporting":
            assert scenario.check, f"{scenario.id} can only be scored on the agent's own word"
