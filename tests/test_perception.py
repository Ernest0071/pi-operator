"""The distiller is the component everything else trusts, so it gets the most tests."""

import pytest
from playwright.async_api import async_playwright

from pi_operator.browser.perception import Element, Snapshot, diff, distill


@pytest.fixture
async def snapshot(sample_form_url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(sample_form_url)
        yield await distill(page), page
        await browser.close()


async def test_resolves_accessible_names_from_labels(snapshot):
    snap, _ = snapshot
    names = {e.name for e in snap.elements}
    assert {"VIN", "Make", "Mileage", "Asking Price"} <= names, names


async def test_excludes_hidden_elements(snapshot):
    snap, _ = snapshot
    assert "Hidden Save" not in {e.name for e in snap.elements}


async def test_captures_state_and_options(snapshot):
    snap, _ = snapshot
    vin = next(e for e in snap.elements if e.name == "VIN")
    assert vin.state.get("required") is True

    make = next(e for e in snap.elements if e.name == "Make")
    assert make.role == "combobox"
    assert "Toyota" in (make.options or [])
    assert make.value == "Honda"

    cert = next(e for e in snap.elements if e.name == "Certified Pre-Owned")
    assert cert.state.get("checked") is True


async def test_context_disambiguates_same_named_controls(snapshot):
    snap, _ = snapshot
    contexts = {e.name: e.context for e in snap.elements}
    assert contexts["VIN"] == "Identification"
    assert contexts["Asking Price"] == "Pricing"


async def test_alerts_are_surfaced(snapshot):
    snap, _ = snapshot
    assert any("VIN is required" in a for a in snap.alerts)


async def test_tables_keep_structure(snapshot):
    snap, _ = snapshot
    assert snap.tables, "expected the recent-intake table"
    table = snap.tables[0]
    assert table.headers == ["Stock", "Model", "Price"]
    assert ["A1", "Civic", "18500"] in table.rows


async def test_ref_resolves_to_the_perceived_element(snapshot):
    snap, page = snapshot
    vin = next(e for e in snap.elements if e.name == "VIN")
    await page.fill(f'[data-pi-ref="{vin.ref}"]', "JH4KA7561PC008269")
    after = await distill(page)
    changed = next(e for e in after.elements if e.name == "VIN")
    assert changed.value == "JH4KA7561PC008269"


async def test_diff_reports_only_what_changed(snapshot):
    snap, page = snapshot
    vin = next(e for e in snap.elements if e.name == "VIN")
    await page.fill(f'[data-pi-ref="{vin.ref}"]', "ABC")
    after = await distill(page)
    text = diff(snap, after)
    assert "CHANGED" in text
    assert "Mileage" not in text, "unchanged fields should not appear in a diff"


def test_digest_is_stable_and_value_sensitive():
    a = Snapshot(url="u", elements=[Element(ref="e0", role="textbox", name="VIN", value="")])
    b = Snapshot(url="u", elements=[Element(ref="e9", role="textbox", name="VIN", value="")])
    c = Snapshot(url="u", elements=[Element(ref="e0", role="textbox", name="VIN", value="X")])
    assert a.digest == b.digest, "digest must not depend on ref numbering"
    assert a.digest != c.digest, "digest must change when a value changes"


def test_diff_detects_no_change():
    a = Snapshot(url="u", elements=[Element(ref="e0", role="button", name="Save")])
    assert "NO VISIBLE CHANGE" in diff(a, a)
