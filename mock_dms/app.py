"""Mock dealership management system — an eval fixture, not a product.

Realism choices are deliberate. This app is *hostile to automation* in the same
ways a real legacy DMS is:

* element ids are regenerated on every render, so an agent that memorises
  ``#save_btn`` breaks immediately and one that resolves by accessible name does not
* saves validate server-side and re-render with field-level errors
* the deal wizard keeps state across three requests
* submitting a deal raises a native confirm() dialog
* sessions expire

None of that is decoration: each one is a failure mode the operator has to
handle, and the eval harness turns each one on deliberately.
"""

from __future__ import annotations

import asyncio
import csv
import io
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from mock_dms import db
from mock_dms.faults import FAULTS, reset_faults

BASE = Path(__file__).parent
@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.ensure()
    yield


app = FastAPI(title="Mock DMS", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE / "templates"))

# Element ids churn on every render — automation must not depend on them.
templates.env.globals["rid"] = lambda: secrets.token_hex(4)
templates.env.globals["label"] = lambda text: FAULTS.label(text)

SESSIONS: dict[str, str] = {}
WIZARD: dict[str, dict[str, Any]] = {}


# ----------------------------------------------------------------- plumbing

@app.middleware("http")
async def fault_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/_") or path.startswith("/static"):
        return await call_next(request)

    FAULTS.request_count += 1

    if FAULTS.latency_ms:
        await asyncio.sleep(FAULTS.latency_ms / 1000)

    if FAULTS.fail_next_requests > 0:
        FAULTS.fail_next_requests -= 1
        return HTMLResponse("<h1>500 Internal Server Error</h1>", status_code=500)

    if FAULTS.expire_session_after and FAULTS.request_count > FAULTS.expire_session_after:
        SESSIONS.clear()

    return await call_next(request)


def current_user(request: Request) -> str | None:
    token = request.cookies.get("dms_session")
    return SESSIONS.get(token or "")


def require_login(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return None


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", current_user(request))
    return templates.TemplateResponse(request, template, ctx)


# --------------------------------------------------------------------- auth

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    rows = db.query(
        "SELECT * FROM users WHERE username = ? AND password = ?", (username, password)
    )
    if not rows:
        return render(request, "login.html", error="Invalid username or password.")
    token = secrets.token_hex(16)
    SESSIONS[token] = username
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("dms_session", token, httponly=True)
    return response


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("dms_session")
    SESSIONS.pop(token or "", None)
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if (redirect := require_login(request)):
        return redirect
    stats = {
        "vehicles": db.query("SELECT COUNT(*) c FROM vehicles")[0]["c"],
        "customers": db.query("SELECT COUNT(*) c FROM customers")[0]["c"],
        "open_deals": db.query("SELECT COUNT(*) c FROM deals WHERE status != 'funded'")[0]["c"],
    }
    return render(request, "dashboard.html", stats=stats)


# ---------------------------------------------------------------- inventory

@app.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request, page: int = 1, q: str = ""):
    if (redirect := require_login(request)):
        return redirect
    per_page = 4  # small on purpose: forces pagination handling
    where, args = ("WHERE make LIKE ? OR model LIKE ? OR vin LIKE ?",
                   (f"%{q}%", f"%{q}%", f"%{q}%")) if q else ("", ())
    total = db.query(f"SELECT COUNT(*) c FROM vehicles {where}", args)[0]["c"]
    rows = db.query(
        f"SELECT * FROM vehicles {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, per_page, (page - 1) * per_page),
    )
    return render(request, "inventory.html", vehicles=rows, page=page, q=q,
                  pages=max(1, -(-total // per_page)), total=total)


@app.get("/inventory/new", response_class=HTMLResponse)
def vehicle_new(request: Request):
    if (redirect := require_login(request)):
        return redirect
    return render(request, "vehicle_form.html", errors={}, values={})


@app.post("/inventory/new")
async def vehicle_create(request: Request):
    if (redirect := require_login(request)):
        return redirect
    form = dict(await request.form())
    errors: dict[str, str] = {}

    for field in ("vin", "make", "model"):
        if not str(form.get(field, "")).strip():
            errors[field] = f"{field.upper() if field == 'vin' else field.title()} is required."

    vin = str(form.get("vin", "")).strip().upper()
    if vin and len(vin) != 17:
        errors["vin"] = "VIN must be exactly 17 characters."
    if vin and db.query("SELECT 1 FROM vehicles WHERE vin = ?", (vin,)):
        errors["vin"] = f"A vehicle with VIN {vin} already exists."

    for field in ("mileage", "price", "year"):
        raw = str(form.get(field, "")).strip()
        if raw and not raw.replace(".", "", 1).isdigit():
            errors[field] = f"{field.title()} must be a number."

    # Injected fault: reject an otherwise-valid save, as a server rule would.
    if not errors and FAULTS.fail_next_saves > 0:
        FAULTS.fail_next_saves -= 1
        errors["mileage"] = FAULTS.validation_message

    if errors:
        return render(request, "vehicle_form.html", errors=errors, values=form)

    stock_no = form.get("stock_no") or f"STK{secrets.randbelow(9000) + 1000}"
    now = time.time()
    vehicle_id = db.execute(
        "INSERT INTO vehicles (stock_no, vin, year, make, model, trim, mileage, condition, "
        "price, status, acquired_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (stock_no, vin, _int(form.get("year")), form.get("make", "").strip(),
         form.get("model", "").strip(), form.get("trim", ""), _int(form.get("mileage")),
         form.get("condition", "Good"), _float(form.get("price")),
         form.get("status", "listed"), now, now),
    )
    return RedirectResponse(f"/inventory/{vehicle_id}", status_code=303)


@app.get("/inventory/{vehicle_id}", response_class=HTMLResponse)
def vehicle_detail(request: Request, vehicle_id: int):
    if (redirect := require_login(request)):
        return redirect
    rows = db.query("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))
    if not rows:
        return HTMLResponse("<h1>404 Vehicle not found</h1>", status_code=404)
    return render(request, "vehicle_detail.html", v=rows[0])


# ---------------------------------------------------------------- customers

@app.get("/customers", response_class=HTMLResponse)
def customers(request: Request):
    if (redirect := require_login(request)):
        return redirect
    return render(request, "customers.html",
                  customers=db.query("SELECT * FROM customers ORDER BY id DESC"))


@app.get("/customers/new", response_class=HTMLResponse)
def customer_new(request: Request):
    if (redirect := require_login(request)):
        return redirect
    return render(request, "customer_form.html", errors={}, values={})


@app.post("/customers/new")
async def customer_create(request: Request):
    if (redirect := require_login(request)):
        return redirect
    form = dict(await request.form())
    errors = {}
    if not str(form.get("name", "")).strip():
        errors["name"] = "Customer name is required."
    email = str(form.get("email", "")).strip()
    if email and "@" not in email:
        errors["email"] = "Enter a valid email address."
    if errors:
        return render(request, "customer_form.html", errors=errors, values=form)

    db.execute(
        "INSERT INTO customers (name, email, phone, customer_group, created_at) VALUES (?,?,?,?,?)",
        (form["name"].strip(), email, form.get("phone", ""),
         form.get("customer_group", "Retail"), time.time()),
    )
    return RedirectResponse("/customers", status_code=303)


# -------------------------------------------------------------------- deals

@app.get("/deals", response_class=HTMLResponse)
def deals(request: Request):
    if (redirect := require_login(request)):
        return redirect
    rows = db.query(
        "SELECT d.*, c.name customer_name, v.stock_no, v.make, v.model "
        "FROM deals d LEFT JOIN customers c ON c.id = d.customer_id "
        "LEFT JOIN vehicles v ON v.id = d.vehicle_id ORDER BY d.id DESC"
    )
    return render(request, "deals.html", deals=rows)


@app.get("/deals/new", response_class=HTMLResponse)
def deal_wizard(request: Request, step: int = 1):
    """Three-step wizard holding state server-side across requests."""
    if (redirect := require_login(request)):
        return redirect
    token = request.cookies.get("dms_session", "")
    draft = WIZARD.setdefault(token, {})

    if step == 1:
        return render(request, "deal_step1.html", step=1,
                      customers=db.query("SELECT * FROM customers ORDER BY name"), draft=draft)
    if step == 2:
        return render(request, "deal_step2.html", step=2,
                      vehicles=db.query("SELECT * FROM vehicles WHERE status = 'listed'"),
                      draft=draft)
    vehicle = db.query("SELECT * FROM vehicles WHERE id = ?", (draft.get("vehicle_id", 0),))
    return render(request, "deal_step3.html", step=3, draft=draft,
                  vehicle=vehicle[0] if vehicle else None, errors={})


@app.post("/deals/new")
async def deal_wizard_post(request: Request, step: int = 1):
    if (redirect := require_login(request)):
        return redirect
    token = request.cookies.get("dms_session", "")
    draft = WIZARD.setdefault(token, {})
    form_data = await request.form()   # read once; the body cannot be re-read
    form = dict(form_data)

    if step == 1:
        if not form.get("customer_id"):
            return render(request, "deal_step1.html", step=1,
                          customers=db.query("SELECT * FROM customers ORDER BY name"),
                          draft=draft, error="Select a customer to continue.")
        draft["customer_id"] = _int(form["customer_id"])
        return RedirectResponse("/deals/new?step=2", status_code=303)

    if step == 2:
        if not form.get("vehicle_id"):
            return render(request, "deal_step2.html", step=2,
                          vehicles=db.query("SELECT * FROM vehicles WHERE status = 'listed'"),
                          draft=draft, error="Select a vehicle to continue.")
        draft["vehicle_id"] = _int(form["vehicle_id"])
        return RedirectResponse("/deals/new?step=3", status_code=303)

    vehicle = db.query("SELECT * FROM vehicles WHERE id = ?", (draft.get("vehicle_id", 0),))
    if not vehicle or not draft.get("customer_id"):
        return RedirectResponse("/deals/new?step=1", status_code=303)

    products = form_data.getlist("fi_products")
    fi_total = float(len(products)) * 750.0
    vehicle_price = float(vehicle[0]["price"] or 0)

    deal_id = db.execute(
        "INSERT INTO deals (customer_id, vehicle_id, fi_products, vehicle_price, fi_total, "
        "total, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (draft["customer_id"], draft["vehicle_id"], ",".join(products), vehicle_price,
         fi_total, vehicle_price + fi_total, "submitted_to_finance", time.time()),
    )
    WIZARD.pop(token, None)
    return RedirectResponse(f"/deals?submitted={deal_id}", status_code=303)


# ------------------------------------------------------------------ service

@app.get("/service", response_class=HTMLResponse)
def service(request: Request):
    if (redirect := require_login(request)):
        return redirect
    rows = db.query(
        "SELECT s.*, v.stock_no, v.make, v.model FROM service_orders s "
        "LEFT JOIN vehicles v ON v.id = s.vehicle_id ORDER BY s.id DESC"
    )
    return render(request, "service.html", orders=rows)


# ------------------------------------------------------------------ reports

def _aging_rows() -> list[dict[str, Any]]:
    now = time.time()
    rows = db.query("SELECT * FROM vehicles ORDER BY acquired_at ASC")
    out = []
    for row in rows:
        days = int((now - (row["acquired_at"] or now)) / 86400)
        out.append({
            "stock_no": row["stock_no"], "vin": row["vin"],
            "vehicle": f"{row['year']} {row['make']} {row['model']}",
            "days_in_stock": days, "price": row["price"],
            "bucket": "0-30" if days <= 30 else "31-60" if days <= 60 else
                      "61-90" if days <= 90 else "91-180" if days <= 180 else "180+",
        })
    return out


@app.get("/reports/aging", response_class=HTMLResponse)
def aging_report(request: Request, min_days: int = 0):
    if (redirect := require_login(request)):
        return redirect
    rows = [r for r in _aging_rows() if r["days_in_stock"] >= min_days]
    return render(request, "aging.html", rows=rows, min_days=min_days)


@app.get("/reports/aging.csv")
def aging_csv(request: Request, min_days: int = 0):
    if (redirect := require_login(request)):
        return redirect
    rows = [r for r in _aging_rows() if r["days_in_stock"] >= min_days]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()) if rows else ["stock_no"])
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory_aging.csv"},
    )


# ------------------------------------------- control plane (evals only)

@app.post("/api/_reset")
def api_reset(seed: bool = True):
    db.reset(seed=seed)
    SESSIONS.clear()
    WIZARD.clear()
    reset_faults()
    return {"ok": True}


@app.get("/api/_fault")
def api_get_fault():
    return FAULTS.snapshot()


@app.post("/api/_fault")
async def api_set_fault(request: Request):
    payload = await request.json()
    for key, value in payload.items():
        if hasattr(FAULTS, key):
            setattr(FAULTS, key, value)
    return FAULTS.snapshot()


@app.post("/api/_verify")
async def api_verify(request: Request):
    """Out-of-band assertion endpoint used by the verifier and eval harness.

    Deliberately not reachable from the pages the agent drives, so a mis-click
    can never verify itself.

    Payload: {"table": "vehicles", "filters": {...}, "expect": {...}}
    """
    payload = await request.json()
    table = payload.get("table")
    filters: dict[str, Any] = payload.get("filters", {})
    expect: dict[str, Any] = payload.get("expect", {})

    if table not in {"vehicles", "customers", "deals", "service_orders"}:
        return JSONResponse({"passed": False, "detail": f"unknown table {table!r}"}, 400)

    clauses = " AND ".join(f"{k} = ?" for k in filters) or "1=1"
    rows = db.query(f"SELECT * FROM {table} WHERE {clauses}", tuple(filters.values()))
    if not rows:
        return {"passed": False, "detail": f"no row in {table} matching {filters}"}

    row = rows[0]
    mismatches = [
        f"{k}: expected {v!r}, found {row.get(k)!r}"
        for k, v in expect.items()
        if str(row.get(k, "")).strip().lower() != str(v).strip().lower()
    ]
    if mismatches:
        return {"passed": False, "detail": "; ".join(mismatches)}
    return {"passed": True, "detail": f"{table} row {row.get('id')} matches {filters} and {expect}"}


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
