from pi_operator.browser.tools import ALL_TOOLS, HandleDialog, ToolRegistry


def test_schemas_generate_from_models():
    schemas = {s["name"]: s for s in ToolRegistry().schemas()}
    assert "click" in schemas
    props = schemas["select"]["input_schema"]["properties"]
    assert set(props) == {"ref", "option"}
    assert schemas["select"]["input_schema"]["required"] == ["ref", "option"]


def test_every_tool_declares_risk_metadata():
    for tool in ALL_TOOLS:
        assert isinstance(tool.mutates_state, bool)
        assert isinstance(tool.reversible, bool)
        assert tool.risk in {"low", "medium", "high"}
        assert tool.tool_description, f"{tool.tool_name} has no description"


def test_irreversible_tools_are_flagged():
    assert HandleDialog.reversible is False
    assert HandleDialog.risk == "high"


def test_registry_subset_restricts_surface():
    subset = ToolRegistry().subset(["observe", "navigate"])
    assert set(subset.tools) == {"observe", "navigate"}


def test_unknown_tool_raises():
    import pytest

    with pytest.raises(KeyError):
        ToolRegistry().build("definitely_not_a_tool", {})


def test_build_validates_arguments():
    import pytest

    with pytest.raises(Exception):
        ToolRegistry().build("click", {})  # ref is required
    assert ToolRegistry().build("click", {"ref": "e1"}).ref == "e1"
