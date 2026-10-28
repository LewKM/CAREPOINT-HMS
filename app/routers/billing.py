from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import (
    ChargeCategory, Invoice, InvoiceItem, InvoiceStatus, Patient, Payment, PaymentMethod, User, utcnow,
)
from ..security import require
from ..services.audit import audit
from ..services.billing import charge, invoice_for, refresh_status
from ..templating import flash, redirect, render
from ..utils import blank, day_bounds, get_or_404, paginate, parse_date, parse_decimal, parse_int, today_local

router = APIRouter(prefix="/billing")


@router.get("")
def index(request: Request, status: str = "unpaid", q: str = "", page: int = 1,
          user: User = Depends(require("billing.view")), db: Session = Depends(get_db)):
    stmt = select(Invoice).join(Patient).options(joinedload(Invoice.patient)).order_by(Invoice.id.desc())
    if status == "unpaid":
        stmt = stmt.where(Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]))
    elif status in InvoiceStatus._value2member_map_:
        stmt = stmt.where(Invoice.status == status)
    else:
        status = "all"
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Invoice.number.ilike(like), Patient.mrn.ilike(like), Patient.first_name.ilike(like),
                              Patient.last_name.ilike(like), Patient.phone.ilike(like)))
    return render(request, "billing/index.html", page=paginate(db, stmt, page), status=status, q=q)


def _invoice(db: Session, invoice_id: int) -> Invoice:
    inv = db.scalar(select(Invoice).options(joinedload(Invoice.patient), joinedload(Invoice.encounter))
                    .where(Invoice.id == invoice_id))
    if not inv:
        raise HTTPException(404)
    return inv


@router.get("/invoices/{invoice_id}")
def invoice(request: Request, invoice_id: int, user: User = Depends(require("billing.view")),
            db: Session = Depends(get_db)):
    inv = _invoice(db, invoice_id)
    cashiers = {u.id: u for u in db.scalars(select(User).where(User.id.in_([p.received_by_id for p in inv.payments if p.received_by_id])))}
    return render(request, "billing/invoice.html", inv=inv, patient=inv.patient, cashiers=cashiers,
                  categories=list(ChargeCategory), methods=list(PaymentMethod))


@router.get("/invoices/{invoice_id}/print")
def print_invoice(request: Request, invoice_id: int, user: User = Depends(require("billing.view")),
                  db: Session = Depends(get_db)):
    inv = _invoice(db, invoice_id)
    audit(db, request, user, "invoice.printed", "invoice", inv.id)
    db.commit()
    return render(request, "billing/print.html", inv=inv, patient=inv.patient)


@router.post("/patients/{patient_id}/invoice")
def open_for_patient(request: Request, patient_id: int, user: User = Depends(require("billing.charge")),
                     db: Session = Depends(get_db)):
    """A bill not tied to a visit — e.g. a walk-in buying a service or a records fee."""
    patient = get_or_404(db, Patient, patient_id)
    inv = invoice_for(db, patient.id, None)
    audit(db, request, user, "invoice.opened", "invoice", inv.id)
    db.commit()
    return redirect(f"/billing/invoices/{inv.id}")


def _editable(db: Session, invoice_id: int) -> Invoice:
    inv = db.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update())
    if not inv:
        raise HTTPException(404)
    if inv.status == InvoiceStatus.VOID:
        raise HTTPException(409, "This invoice has been voided.")
    return inv


@router.post("/invoices/{invoice_id}/items")
def add_item(request: Request, invoice_id: int, category: Annotated[str, Form()] = "",
             description: Annotated[str, Form()] = "", quantity: Annotated[str, Form()] = "1",
             unit_price: Annotated[str, Form()] = "", user: User = Depends(require("billing.charge")),
             db: Session = Depends(get_db)):
    inv = _editable(db, invoice_id)
    try:
        qty = parse_int(quantity, minimum=1, maximum=1000) or 1
        price = parse_decimal(unit_price, minimum=None)
    except ValueError:
        price = None
    if category not in ChargeCategory._value2member_map_ or not description.strip() or price is None or price == 0:
        flash(request, "Choose a category and enter a description and a non-zero price (negative for a credit).", "error")
        return redirect(f"/billing/invoices/{inv.id}")
    inv.items.append(InvoiceItem(category=ChargeCategory(category), description=description.strip()[:255],
                                 quantity=qty, unit_price=price, created_by_id=user.id, created_at=utcnow()))
    refresh_status(inv)
    audit(db, request, user, "invoice.item_added", "invoice", inv.id, f"{description.strip()} x{qty} @ {price}")
    db.commit()
    flash(request, "Charge added.")
    return redirect(f"/billing/invoices/{inv.id}")


@router.post("/invoices/{invoice_id}/payments")
def record_payment(request: Request, invoice_id: int, amount: Annotated[str, Form()] = "",
                   method: Annotated[str, Form()] = "", reference: Annotated[str, Form()] = "",
                   user: User = Depends(require("billing.collect")), db: Session = Depends(get_db)):
    inv = _editable(db, invoice_id)
    try:
        amt = parse_decimal(amount)
    except ValueError:
        amt = None
    if not amt or amt <= 0:
        flash(request, "Enter an amount greater than zero.", "error")
        return redirect(f"/billing/invoices/{inv.id}")
    if amt > inv.balance:
        flash(request, f"That's more than the balance of {inv.balance:,.2f}.", "error")
        return redirect(f"/billing/invoices/{inv.id}")
    if method not in PaymentMethod._value2member_map_:
        flash(request, "Choose a payment method.", "error")
        return redirect(f"/billing/invoices/{inv.id}")
    if method in ("mpesa", "card", "insurance", "bank") and not reference.strip():
        flash(request, "A transaction or claim reference is required for non-cash payments.", "error")
        return redirect(f"/billing/invoices/{inv.id}")
    inv.payments.append(Payment(amount=amt, method=PaymentMethod(method), reference=blank(reference),
                                received_by_id=user.id, received_at=utcnow()))
    refresh_status(inv)
    audit(db, request, user, "payment.received", "invoice", inv.id, f"{amt} {method}")
    db.commit()
    flash(request, f"Payment of {amt:,.2f} received." + (" Invoice fully paid." if inv.status == InvoiceStatus.PAID else ""))
    return redirect(f"/billing/invoices/{inv.id}")


@router.post("/invoices/{invoice_id}/void")
def void(request: Request, invoice_id: int, reason: Annotated[str, Form()] = "",
         user: User = Depends(require("billing.void")), db: Session = Depends(get_db)):
    inv = _editable(db, invoice_id)
    if inv.payments:
        flash(request, "This invoice has payments recorded — add a credit line instead of voiding it.", "error")
        return redirect(f"/billing/invoices/{inv.id}")
    if not reason.strip():
        flash(request, "Give a reason for voiding.", "error")
        return redirect(f"/billing/invoices/{inv.id}")
    inv.status, inv.void_reason = InvoiceStatus.VOID, reason.strip()[:255]
    audit(db, request, user, "invoice.voided", "invoice", inv.id, inv.void_reason)
    db.commit()
    flash(request, "Invoice voided.")
    return redirect(f"/billing/invoices/{inv.id}")


@router.get("/payments")
def payments(request: Request, day: str = "", user: User = Depends(require("billing.view")),
             db: Session = Depends(get_db)):
    try:
        the_day = parse_date(day) or today_local()
    except ValueError:
        the_day = today_local()
    start, end = day_bounds(the_day)
    rows = db.scalars(select(Payment).options(joinedload(Payment.invoice).joinedload(Invoice.patient),
                                              joinedload(Payment.received_by))
                      .where(Payment.received_at >= start, Payment.received_at < end).order_by(Payment.received_at)).all()
    by_method: dict[PaymentMethod, Decimal] = {}
    by_cashier: dict[str, Decimal] = {}
    for p in rows:
        by_method[p.method] = by_method.get(p.method, Decimal(0)) + p.amount
        name = p.received_by.full_name if p.received_by else "—"
        by_cashier[name] = by_cashier.get(name, Decimal(0)) + p.amount
    return render(request, "billing/payments.html", payments=rows, day=the_day, by_method=by_method,
                  by_cashier=by_cashier, total=sum(by_method.values(), Decimal(0)))
