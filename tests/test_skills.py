import pytest

from pi_operator.browser.perception import Element, Snapshot
from pi_operator.graph.state import StepRecord
from pi_operator.skills.base import ElementDescriptor, RecordedStep, resolve
from pi_operator.skills.registry import TrajectoryCompiler

SNAP = Snapshot(url="u", elements=[
    Element(ref="e1", role="button", name="Save Vehicle", context="Identification"),
    Element(ref="e2", role="textbox", name="VIN Number", context="Identification"),
    Element(ref="e3", role="button", name="Cancel"),
])


@pytest.mark.parametrize("want,tier,healed", [
    (ElementDescriptor(role="button", name="Save Vehicle", context="Identification"), "exact", False),
    (ElementDescriptor(role="button", name="Save Vehicle", context="Elsewhere"), "role_name", False),
    (ElementDescriptor(role="link", name="Save Vehicle"), "name", True),
    (ElementDescriptor(role="textbox", name="vin  number"), "normalized", True),
])
def test_resolution_ladder(want, tier, healed):
    result = resolve(SNAP, want)
    assert result.tier == tier
    assert result.healed is healed
    assert result.ref is not None


def test_label_extension_heals_by_containment():
    snap = Snapshot(url="u", elements=[Element(ref="e1", role="button", name="Save Vehicle Record")])
    result = resolve(snap, ElementDescriptor(role="button", name="Save Vehicle"))
    assert result.tier == "contains"
    assert result.ref == "e1"


def test_unrelated_rename_refuses_rather_than_guessing():
    """The important negative case: a wrong click is worse than a clean failure."""
    snap = Snapshot(url="u", elements=[
        Element(ref="e1", role="button", name="Commit Unit To Stock"),
        Element(ref="e2", role="button", name="Delete Vehicle"),
    ])
    result = resolve(snap, ElementDescriptor(role="button", name="Save Vehicle"))
    assert result.ref is None
    assert result.tier == "none"


def test_missing_element_returns_none():
    assert resolve(SNAP, ElementDescriptor(role="button", name="Publish")).ref is None


def test_step_binding_substitutes_and_validates():
    step = RecordedStep(tool="type", args={"text": "{{vin}}", "append": False})
    assert step.bind({"vin": "JH4"}) == {"text": "JH4", "append": False}
    with pytest.raises(KeyError):
        step.bind({})


def test_trajectory_compiles_to_parameterised_skill():
    history = [
        StepRecord(index=0, node="n", tool="navigate", args={"url": "/inventory/new"}, ok=True),
        StepRecord(index=1, node="n", tool="observe", ok=True),
        StepRecord(index=2, node="n", tool="type", args={"ref": "e3", "text": "JH4KA7561PC008269"},
                   element={"role": "textbox", "name": "VIN", "context": "ID"}, ok=True),
        StepRecord(index=3, node="n", tool="click", args={"ref": "e9"}, ok=False),
        StepRecord(index=4, node="n", tool="done", ok=True),
    ]
    skill = TrajectoryCompiler.compile(
        name="add_vehicle", description="", target_name="mockdms", history=history,
        params={"vin": "JH4KA7561PC008269"},
    )
    tools = [s.tool for s in skill.steps]
    assert tools == ["navigate", "type"], "observe/done/failed steps must be dropped"
    assert skill.steps[1].args["text"] == "{{vin}}"
    assert "ref" not in skill.steps[1].args, "refs are not stable across runs"
    assert skill.steps[1].element.name == "VIN"


def test_longer_params_substitute_first():
    """A short param value that is a substring of a longer one must not clobber it."""
    history = [StepRecord(index=0, node="n", tool="type", args={"text": "2021 Nissan"}, ok=True)]
    skill = TrajectoryCompiler.compile(
        name="s", description="", target_name="t", history=history,
        params={"year": "2021", "full": "2021 Nissan"},
    )
    assert skill.steps[0].args["text"] == "{{full}}"
