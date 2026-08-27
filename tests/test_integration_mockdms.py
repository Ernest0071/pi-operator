"""Live integration tests against the mock DMS.

Run with the fixture up:  `pi dms` in one shell, `pytest -m live` in another.

These exercise the whole lower stack — perception, session, tools, guardrails,
self-healing resolution — against a real browser and a real server, with no LLM
involved. That separation is deliberate: when a run misbehaves, these tests tell
you whether the machinery or the model is at fault.
"""

import httpx
import pytest

from pi_operator.browser.session import BrowserSession
from pi_operator.browser.tools import ToolRegistry
from pi_operator.skills.base import ElementDescriptor, resolve
from pi_operator.targets import get_target

pytestmark = pytest.mark.live

BASE = "http://127.0.0.1:8080"


def _running() -> bool:
    try:
        return httpx.get(f"{BASE}/login", timeout=2).status_code == 200
    except Exception:
        return False


pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not _running(), reason="mock DMS not running on :8080")]


async def _reset(**faults):
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{BASE}/api/_reset")
        if faults:
            await client.post(f"{BASE}/api/_fault", json=faults)


async def _verify(payload):
    async with httpx.AsyncClient(timeout=20) as client:
        return (await client.post(f"{BASE}/api/_verify", json=payload)).json()


@pytest.fixture
async def session():
    target = get_target("mockdms", base_url=BASE, username="operator", password="operator")
    await _reset()
    browser = await BrowserSession(headless=True, base_url=BASE).start()
    await target.login(browser)
    yield browser, target
    await browser.close()


def _ref(snap, role, name):
    return next(e.ref for e in snap.elements if e.role == role and e.name == name)


async def test_login_and_perceive_form(session):
    browser, target = session
    assert await target.is_authenticated(browser)
    await browser.goto("/inventory/new")
    snap = await browser.observe()
    names = {e.name for e in snap.elements}
    assert {"VIN", "Make", "Model", "Mileage", "Asking Price", "Save Vehicle"} <= names


async def test_perception_compresses_the_page(session):
    browser, _ = session
    await browser.goto("/inventory/new")
    snap = await browser.observe()
    raw = len(await browser.page.content())
    rendered = len(snap.render())
    assert rendered < raw / 3, f"distilled {rendered} vs raw {raw}"


async def test_fill_and_save_lands_in_the_database(session):
    browser, _ = session
    registry = ToolRegistry()
    vin = "1N4AL3AP8JC000999"

    await browser.goto("/inventory/new")
    snap = await browser.observe()
    for role, name, value in [
        ("textbox", "VIN", vin), ("textbox", "Year", "2021"),
        ("textbox", "Make", "Nissan"), ("textbox", "Model", "Altima"),
        ("textbox", "Mileage", "33000"), ("textbox", "Asking Price", "19250"),
    ]:
        result = await registry.build("type", {"ref": _ref(snap, role, name), "text": value}).run(browser, snap)
        assert result.ok, result.message

    await registry.build("click", {"ref": _ref(snap, "button", "Save Vehicle")}).run(browser, snap)

    check = await _verify({"table": "vehicles", "filters": {"vin": vin},
                           "expect": {"make": "Nissan", "mileage": "33000"}})
    assert check["passed"], check["detail"]


async def test_native_validation_is_surfaced_not_silent(session):
    """A blocked native submit looks like 'nothing happened' unless we name it."""
    browser, _ = session
    registry = ToolRegistry()
    await browser.goto("/inventory/new")
    snap = await browser.observe()
    await registry.build("type", {"ref": _ref(snap, "textbox", "VIN"), "text": "SHORT"}).run(browser, snap)
    await registry.build("click", {"ref": _ref(snap, "button", "Save Vehicle")}).run(browser, snap)
    after = await browser.observe()
    assert after.alerts, "the operator would see an unexplained no-op"
    assert any("fill out this field" in a.lower() for a in after.alerts)


async def test_server_side_validation_error_is_perceived(session):
    browser, target = session
    registry = ToolRegistry()
    await _reset(fail_next_saves=1)
    await target.login(browser)
    await browser.goto("/inventory/new")
    snap = await browser.observe()
    for role, name, value in [("textbox", "VIN", "1N4AL3AP8JC000888"), ("textbox", "Make", "Kia"),
                              ("textbox", "Model", "Rio"), ("textbox", "Mileage", "12000")]:
        await registry.build("type", {"ref": _ref(snap, role, name), "text": value}).run(browser, snap)
    await registry.build("click", {"ref": _ref(snap, "button", "Save Vehicle")}).run(browser, snap)
    alerts = (await browser.observe()).alerts
    assert any("whole number of miles" in a for a in alerts), alerts


async def test_selector_healing_absorbs_a_renamed_button(session):
    browser, target = session
    await _reset(mutate_labels=True)
    await target.login(browser)
    await browser.goto("/inventory/new")
    snap = await browser.observe()

    resolution = resolve(snap, ElementDescriptor(role="button", name="Save Vehicle"))
    assert resolution.ref is not None
    assert resolution.healed
    assert snap.by_ref(resolution.ref).name == "Save Vehicle Record"


async def test_drastic_rename_refuses_rather_than_misclicking(session):
    browser, target = session
    await _reset(drastic_labels=True)
    await target.login(browser)
    await browser.goto("/inventory/new")
    snap = await browser.observe()
    assert resolve(snap, ElementDescriptor(role="button", name="Save Vehicle")).ref is None


async def test_confirm_dialog_is_denied_by_default_and_blocks_the_action(session):
    """The guardrail must actually prevent the irreversible action, not just log it."""
    browser, _ = session
    registry = ToolRegistry()

    await browser.goto("/deals/new?step=1")
    snap = await browser.observe()
    await registry.build("select", {"ref": next(e.ref for e in snap.elements if e.role == "combobox"),
                                    "option": "Marcus Webb"}).run(browser, snap)
    await registry.build("click", {"ref": next(e.ref for e in snap.elements if e.role == "button")}).run(browser, snap)

    snap = await browser.observe()
    vehicle = next(e for e in snap.elements if e.role == "combobox")
    await registry.build("select", {"ref": vehicle.ref, "option": vehicle.options[1]}).run(browser, snap)
    await registry.build("click", {"ref": next(e.ref for e in snap.elements if e.role == "button")}).run(browser, snap)

    snap = await browser.observe()
    submit = next(e for e in snap.elements if e.role == "button" and "Submit" in e.name)
    await registry.build("click", {"ref": submit.ref}).run(browser, snap)

    assert browser.pending_dialogs, "the confirm() dialog should have been recorded"
    assert not (await _verify({"table": "deals", "filters": {"id": 1}}))["passed"], \
        "dismissing the confirm must prevent the deal from being created"


async def test_stale_ref_fails_loudly(session):
    browser, _ = session
    registry = ToolRegistry()
    await browser.goto("/inventory/new")
    snap = await browser.observe()
    await browser.goto("/inventory")  # everything re-rendered
    with pytest.raises(LookupError):
        await registry.build("click", {"ref": _ref(snap, "button", "Save Vehicle")}).run(browser, snap)


async def test_report_table_is_extracted_with_structure(session):
    browser, _ = session
    await browser.goto("/reports/aging")
    snap = await browser.observe()
    assert snap.tables
    table = snap.tables[0]
    assert len(table.rows) == 6
    assert any("Volkswagen" in " ".join(row) for row in table.rows)


async def test_pagination_is_visible_to_the_operator(session):
    browser, _ = session
    await browser.goto("/inventory")
    snap = await browser.observe()
    assert any(e.name == "Next" for e in snap.elements), "operator must be able to page"
