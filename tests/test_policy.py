from pi_operator.browser.perception import Element, Snapshot
from pi_operator.browser.tools import Click, HandleDialog, Navigate, Observe, Type
from pi_operator.graph.state import RunState, StepRecord
from pi_operator.guardrails.policy import Policy

SNAP = Snapshot(url="http://localhost:8080/deals", elements=[
    Element(ref="e1", role="button", name="Save Draft"),
    Element(ref="e2", role="button", name="Submit for Finance Approval"),
    Element(ref="e3", role="button", name="Delete Deal"),
    Element(ref="e4", role="textbox", name="Asking Price"),
])
POLICY = Policy(allowed_hosts={"localhost:8080"})


def test_benign_action_allowed():
    assert POLICY.assess(Click(ref="e1"), SNAP, RunState(goal="g")).verdict == "allow"


def test_committing_language_escalates():
    assert POLICY.assess(Click(ref="e2"), SNAP, RunState(goal="g")).verdict == "approve"


def test_destructive_language_escalates_as_high_risk():
    decision = POLICY.assess(Click(ref="e3"), SNAP, RunState(goal="g"))
    assert decision.verdict == "approve"
    assert decision.risk == "high"


def test_read_only_action_never_escalates():
    assert POLICY.assess(Observe(), SNAP, RunState(goal="g")).verdict == "allow"


def test_material_amounts_escalate_but_small_ones_do_not():
    big = POLICY.assess(Type(ref="e4", text="45000"), SNAP, RunState(goal="g"))
    small = POLICY.assess(Type(ref="e4", text="1200"), SNAP, RunState(goal="g"))
    assert big.verdict == "approve"
    assert small.verdict == "allow"


def test_irreversible_tool_escalates_regardless_of_element():
    assert POLICY.assess(HandleDialog(accept=True), SNAP, RunState(goal="g")).verdict == "approve"


def test_navigation_off_target_is_denied():
    decision = POLICY.assess(Navigate(url="https://evil.example.com"), SNAP, RunState(goal="g"))
    assert decision.verdict == "deny"


def test_budgets_stop_the_run():
    state = RunState(goal="g")
    for i in range(5):
        state.record(StepRecord(index=i, node="n"))
    assert Policy(max_steps=3).check_budget(state) is not None
    assert Policy(max_steps=99).check_budget(state) is None


def test_unattended_mode_downgrades_gates_but_not_denials():
    unattended = Policy(allowed_hosts={"localhost:8080"}, require_approval=False)
    assert unattended.assess(Click(ref="e3"), SNAP, RunState(goal="g")).verdict == "allow"
    assert unattended.assess(
        Navigate(url="https://evil.example.com"), SNAP, RunState(goal="g")
    ).verdict == "deny", "denials are not negotiable, even unattended"
