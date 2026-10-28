from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import LAB_PRIORITY_RANK, ChargeCategory, LabOrder, LabStatus, LabTest, User, utcnow
from ..security import require
from ..services.audit import audit
from ..services.billing import charge
from ..templating import flash, redirect, render
from ..utils import blank, day_bounds, get_or_404, parse_decimal, parse_int

router = APIRouter(prefix="/lab")


@router.get("")
def worklist(request: Request, status: str = "open", user: User = Depends(require("lab.view")),
             db: Session = Depends(get_db)):
    stmt = select(LabOrder).options(joinedload(LabOrder.patient), joinedload(LabOrder.test), joinedload(LabOrder.ordered_by))
    if status == "completed":
        start, end = day_bounds()
        orders = db.scalars(stmt.where(LabOrder.status == LabStatus.COMPLETED, LabOrder.completed_at >= start,
                                       LabOrder.completed_at < end).order_by(LabOrder.completed_at.desc())).all()
    else:
        wanted = {"ordered": [LabStatus.ORDERED], "collected": [LabStatus.COLLECTED]}.get(
            status, [LabStatus.ORDERED, LabStatus.COLLECTED])
        status = status if status in ("ordered", "collected") else "open"
        orders = sorted(db.scalars(stmt.where(LabOrder.status.in_(wanted))).all(),
                        key=lambda o: (LAB_PRIORITY_RANK[o.priority], o.created_at))
    return render(request, "lab/worklist.html", orders=orders, status=status)


@router.get("/orders/{order_id}")
def order(request: Request, order_id: int, user: User = Depends(require("lab.view")), db: Session = Depends(get_db)):
    o = db.scalar(select(LabOrder).options(joinedload(LabOrder.patient), joinedload(LabOrder.test),
                                           joinedload(LabOrder.ordered_by), joinedload(LabOrder.performed_by))
                  .where(LabOrder.id == order_id))
    if not o:
        raise HTTPException(404)
    return render(request, "lab/order.html", o=o, patient=o.patient)


def _open(db: Session, order_id: int, *expected: LabStatus) -> LabOrder:
    o = get_or_404(db, LabOrder, order_id)
    if o.status not in expected:
        raise HTTPException(409, f"This order is already {o.status.label.lower()}.")
    return o


@router.post("/orders/{order_id}/collect")
def collect(request: Request, order_id: int, user: User = Depends(require("lab.process")), db: Session = Depends(get_db)):
    o = _open(db, order_id, LabStatus.ORDERED)
    o.status, o.collected_at, o.collected_by_id = LabStatus.COLLECTED, utcnow(), user.id
    audit(db, request, user, "lab.collected", "lab_order", o.id)
    db.commit()
    flash(request, f"Sample collected for {o.test.name}.")
    return redirect(f"/lab/orders/{o.id}")


@router.post("/orders/{order_id}/result")
def result(request: Request, order_id: int, result_value: Annotated[str, Form()] = "",
           result_notes: Annotated[str, Form()] = "", is_abnormal: Annotated[str, Form()] = "",
           user: User = Depends(require("lab.process")), db: Session = Depends(get_db)):
    o = _open(db, order_id, LabStatus.COLLECTED)
    if not result_value.strip():
        flash(request, "Enter the result.", "error")
        return redirect(f"/lab/orders/{o.id}")
    o.result_value, o.result_notes, o.is_abnormal = result_value.strip(), blank(result_notes), is_abnormal == "on"
    o.status, o.completed_at, o.performed_by_id = LabStatus.COMPLETED, utcnow(), user.id
    audit(db, request, user, "lab.resulted", "lab_order", o.id, "abnormal" if o.is_abnormal else None)
    db.commit()
    flash(request, "Result released to the requesting doctor.")
    return redirect("/lab")


@router.post("/orders/{order_id}/cancel")
def cancel(request: Request, order_id: int, reason: Annotated[str, Form()] = "",
           user: User = Depends(require("lab.process")), db: Session = Depends(get_db)):
    o = _open(db, order_id, LabStatus.ORDERED, LabStatus.COLLECTED)
    if not reason.strip():
        flash(request, "Give a reason (e.g. sample rejected, haemolysed).", "error")
        return redirect(f"/lab/orders/{o.id}")
    o.status, o.result_notes = LabStatus.CANCELLED, f"Cancelled: {reason.strip()}"
    if o.test.price > 0:
        charge(db, patient_id=o.patient_id, encounter_id=o.encounter_id, category=ChargeCategory.LABORATORY,
               description=f"Credit — {o.test.name} cancelled", unit_price=-o.test.price, user_id=user.id)
    audit(db, request, user, "lab.cancelled", "lab_order", o.id, reason.strip())
    db.commit()
    flash(request, "Order cancelled and the charge credited back on the patient's bill.")
    return redirect("/lab")


# ---------------------------------------------------------------- catalogue

@router.get("/tests")
def tests(request: Request, user: User = Depends(require("lab.catalog")), db: Session = Depends(get_db)):
    rows = db.scalars(select(LabTest).order_by(LabTest.category, LabTest.name)).all()
    return render(request, "lab/tests.html", tests=rows, edit=None, errors={})


@router.get("/tests/{test_id}")
def edit_test(request: Request, test_id: int, user: User = Depends(require("lab.catalog")), db: Session = Depends(get_db)):
    rows = db.scalars(select(LabTest).order_by(LabTest.category, LabTest.name)).all()
    return render(request, "lab/tests.html", tests=rows, edit=get_or_404(db, LabTest, test_id), errors={})


@router.post("/tests")
@router.post("/tests/{test_id}")
def save_test(request: Request, test_id: int | None = None, code: Annotated[str, Form()] = "",
              name: Annotated[str, Form()] = "", category: Annotated[str, Form()] = "",
              specimen: Annotated[str, Form()] = "", unit: Annotated[str, Form()] = "",
              reference_range: Annotated[str, Form()] = "", price: Annotated[str, Form()] = "",
              turnaround_hours: Annotated[str, Form()] = "", is_active: Annotated[str, Form()] = "",
              user: User = Depends(require("lab.catalog")), db: Session = Depends(get_db)):
    t = get_or_404(db, LabTest, test_id) if test_id else LabTest()
    errors = {}
    code = code.strip().upper()
    if not code or not name.strip() or not category.strip() or not specimen.strip():
        errors["form"] = "Code, name, category and specimen are required."
    elif db.scalar(select(LabTest.id).where(LabTest.code == code, LabTest.id != (t.id or 0))):
        errors["form"] = f"Code {code} is already used."
    try:
        price_v = parse_decimal(price)
        hours = parse_int(turnaround_hours, minimum=0, maximum=720)
        if price_v is None:
            errors["form"] = errors.get("form") or "Price is required."
    except ValueError:
        errors["form"] = "Price and turnaround must be numbers."
    if errors:
        flash(request, errors["form"], "error")
        return redirect(f"/lab/tests/{t.id}" if t.id else "/lab/tests")
    t.code, t.name, t.category, t.specimen = code, name.strip(), category.strip(), specimen.strip()
    t.unit, t.reference_range, t.price = blank(unit), blank(reference_range), price_v
    t.turnaround_hours = hours or 2
    t.is_active = is_active == "on" if test_id else True
    db.add(t)
    db.flush()
    audit(db, request, user, "lab_test.saved", "lab_test", t.id, code)
    db.commit()
    flash(request, f"{t.name} saved.")
    return redirect("/lab/tests")
