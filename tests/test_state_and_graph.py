from pi_operator.graph.state import RunState, RunStatus, StepRecord
from pi_operator.graph.supervisor import Supervisor
from pi_operator.guardrails.policy import Policy
from pi_operator.targets import get_target


def test_state_round_trips_for_checkpointing():
    state = RunState(goal="g", target="mockdms")
    state.record(StepRecord(index=0, node="navigator", tool="click"))
    restored = RunState.model_validate_json(state.model_dump_json())
    assert restored.run_id == state.run_id
    assert restored.history[0].tool == "click"


def test_oscillation_detected_on_repeated_identical_screens():
    state = RunState(goal="g")
    for i in range(3):
        state.record(StepRecord(index=i, node="n", observation_digest="same"))
    assert state.oscillating()


def test_distinct_screens_are_not_oscillation():
    state = RunState(goal="g")
    for i in range(5):
        state.record(StepRecord(index=i, node="n", observation_digest=f"d{i}"))
    assert not state.oscillating()


def test_consecutive_failures_reset_on_success():
    state = RunState(goal="g")
    state.record(StepRecord(index=0, node="n", ok=False))
    state.record(StepRecord(index=1, node="n", ok=False))
    assert state.consecutive_failures == 2
    state.record(StepRecord(index=2, node="n", ok=True))
    assert state.consecutive_failures == 0


def test_terminal_status():
    assert RunStatus.SUCCEEDED.terminal and RunStatus.FAILED.terminal
    assert not RunStatus.RUNNING.terminal


def _supervisor():
    target = get_target("mockdms")
    return Supervisor(session=None, target=target, planner=None, navigator=None,
                      verifier=None, policy=Policy.for_target(target))


def test_graph_compiles_with_all_nodes():
    graph = _supervisor().build().get_graph()
    nodes = {n for n in graph.nodes if not n.startswith("__")}
    assert nodes == {"setup", "plan", "route", "run_skill", "navigate",
                     "approval_gate", "human_gate", "replan", "verify", "report"}


def _state(**kwargs):
    state = RunState(goal="g")
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def test_routing_sends_gated_actions_to_the_approval_node():
    assert Supervisor.after_navigate(_state(last_outcome="needs_approval")) == "approval_gate"
    assert Supervisor.after_navigate(_state(last_outcome="needs_human")) == "human_gate"


def test_routing_verifies_before_reporting_success():
    assert Supervisor.after_navigate(_state(last_outcome="done")) == "verify"
    assert Supervisor.after_navigate(_state(last_outcome="plan_complete")) == "verify"


def test_oscillation_triggers_replan_then_gives_up():
    state = _state(last_outcome="acted")
    state.digest_counts = {"x": 3}
    assert Supervisor.after_navigate(state) == "replan"
    state.replans = 2
    assert Supervisor.after_navigate(state) == "report", "must not replan forever"
