from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import (
    ChargeCategory, Drug, Prescription, PrescriptionItem, PrescriptionStatus, StockMovement, User, utcnow,
)
from ..security import require
from ..services.audit import audit
from ..services.billing import charge
from ..templating import flash, redirect, render
from ..utils import blank, get_or_404, paginate, parse_date, parse_decimal, parse_int, today_local

router = APIRouter(prefix="/pharmacy")

DRUG_FORMS = ["Tablet", "Capsule", "Syrup", "Suspension", "Injection", "Infusion", "Cream", "Ointment", "Drops",
              "Inhaler", "Sachet", "Suppository", "Other"]


def _move(db: Session, drug: Drug, change: int, reason: str, reference: str | None, user: User) -> None:
    drug.stock_quantity += change
    db.add(StockMovement(drug_id=drug.id, change=change, balance_after=drug.stock_quantity, reason=reason,
                         reference=reference, user_id=user.id, created_at=utcnow()))


@router.get("")
def queue(request: Request, status: str = "pending", user: User = Depends(require("pharmacy.view")),
          db: Session = Depends(get_db)):
    stmt = select(Prescription).options(joinedload(Prescription.patient), joinedload(Prescription.doctor),
                                        joinedload(Prescription.items).joinedload(PrescriptionItem.drug))
    if status == "dispensed":
        stmt = stmt.where(Prescription.status == PrescriptionStatus.DISPENSED).order_by(Prescription.dispensed_at.desc()).limit(50)
    else:
        status = "pending"
        stmt = stmt.where(Prescription.status == PrescriptionStatus.PENDING).order_by(Prescription.created_at)
    return render(request, "pharmacy/queue.html", prescriptions=db.scalars(stmt).unique().all(), status=status)


@router.get("/prescriptions/{rx_id}")
def prescription(request: Request, rx_id: int, user: User = Depends(require("pharmacy.view")),
                 db: Session = Depends(get_db)):
    rx = db.scalar(select(Prescription).options(
        joinedload(Prescription.patient), joinedload(Prescription.doctor), joinedload(Prescription.dispensed_by),
        joinedload(Prescription.items).joinedload(PrescriptionItem.drug)).where(Prescription.id == rx_id))
    if not rx:
        raise HTTPException(404)
    return render(request, "pharmacy/prescription.html", rx=rx, patient=rx.patient)


@router.post("/prescriptions/{rx_id}/dispense")
def dispense(request: Request, rx_id: int, user: User = Depends(require("pharmacy.dispense")),
             db: Session = Depends(get_db)):
    rx = get_or_404(db, Prescription, rx_id)
    if rx.status != PrescriptionStatus.PENDING:
        raise HTTPException(409, f"This prescription is already {rx.status.label.lower()}.")
    # Lock the stock rows so two pharmacists can't dispense the last units twice.
    drugs = {d.id: d for d in db.scalars(select(Drug).where(Drug.id.in_([i.drug_id for i in rx.items])).with_for_update())}
    short = [f"{drugs[i.drug_id].name} (need {i.quantity}, have {drugs[i.drug_id].stock_quantity})"
             for i in rx.items if drugs[i.drug_id].stock_quantity < i.quantity]
    if short:
        flash(request, "Not enough stock: " + "; ".join(short) + ".", "error")
        return redirect(f"/pharmacy/prescriptions/{rx.id}")
    ref = f"RX-{rx.id}"
    for item in rx.items:
        drug = drugs[item.drug_id]
        _move(db, drug, -item.quantity, "dispense", ref, user)
        charge(db, patient_id=rx.patient_id, encounter_id=rx.encounter_id, category=ChargeCategory.PHARMACY,
               description=f"Pharmacy — {drug.label}", unit_price=drug.unit_price, quantity=item.quantity, user_id=user.id)
    rx.status, rx.dispensed_by_id, rx.dispensed_at = PrescriptionStatus.DISPENSED, user.id, utcnow()
    audit(db, request, user, "prescription.dispensed", "prescription", rx.id)
    db.commit()
    flash(request, f"Dispensed to {rx.patient.full_name}. Charges added to the bill.")
    return redirect("/pharmacy")


@router.post("/prescriptions/{rx_id}/cancel")
def cancel(request: Request, rx_id: int, reason: Annotated[str, Form()] = "",
           user: User = Depends(require("pharmacy.dispense")), db: Session = Depends(get_db)):
    rx = get_or_404(db, Prescription, rx_id)
    if rx.status != PrescriptionStatus.PENDING:
        raise HTTPException(409, "Only pending prescriptions can be cancelled.")
    if not reason.strip():
        flash(request, "Give a reason so the prescriber knows why.", "error")
        return redirect(f"/pharmacy/prescriptions/{rx.id}")
    rx.status, rx.cancel_reason = PrescriptionStatus.CANCELLED, reason.strip()[:255]
    audit(db, request, user, "prescription.cancelled", "prescription", rx.id, rx.cancel_reason)
    db.commit()
    flash(request, "Prescription cancelled.")
    return redirect("/pharmacy")


# ---------------------------------------------------------------- inventory

@router.get("/inventory")
def inventory(request: Request, q: str = "", view: str = "all", page: int = 1,
              user: User = Depends(require("pharmacy.inventory")), db: Session = Depends(get_db)):
    stmt = select(Drug).order_by(Drug.name)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Drug.name.ilike(like), Drug.generic_name.ilike(like)))
    if view == "low":
        stmt = stmt.where(Drug.is_active, Drug.stock_quantity <= Drug.reorder_level)
    elif view == "expiring":
        stmt = stmt.where(Drug.is_active, Drug.expiry_date <= today_local() + timedelta(days=60))
    elif view == "inactive":
        stmt = stmt.where(~Drug.is_active)
    else:
        view = "all"
        stmt = stmt.where(Drug.is_active)
    return render(request, "pharmacy/inventory.html", page=paginate(db, stmt, page), q=q, view=view)


class DrugForm(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str = ""
    generic_name: str = ""
    form: str = ""
    strength: str = ""
    unit_price: str = ""
    reorder_level: str = "20"
    expiry_date: str = ""
    opening_stock: str = ""
    is_active: str = ""

    def clean(self, creating: bool) -> tuple[dict, dict]:
        errors, data = {}, {}
        if not self.name:
            errors["name"] = "Required."
        if self.form not in DRUG_FORMS:
            errors["form"] = "Choose a dosage form."
        for field, parser in (("unit_price", parse_decimal), ("reorder_level", parse_int), ("expiry_date", parse_date)):
            try:
                data[field] = parser(getattr(self, field))
            except ValueError:
                errors[field] = "Invalid value."
        if data.get("unit_price") is None and "unit_price" not in errors:
            errors["unit_price"] = "Required."
        if creating:
            try:
                data["opening_stock"] = parse_int(self.opening_stock, minimum=0) or 0
            except ValueError:
                errors["opening_stock"] = "Whole number, 0 or more."
        data.update(name=self.name, generic_name=blank(self.generic_name), form=self.form,
                    strength=blank(self.strength), reorder_level=data.get("reorder_level") or 0,
                    is_active=creating or self.is_active == "on")
        return data, errors


@router.get("/drugs/new")
def new_drug(request: Request, user: User = Depends(require("pharmacy.inventory"))):
    return render(request, "pharmacy/drug_form.html", drug=None, form=DrugForm(), errors={}, forms=DRUG_FORMS)


@router.post("/drugs/new")
def create_drug(request: Request, form: Annotated[DrugForm, Form()], user: User = Depends(require("pharmacy.inventory")),
                db: Session = Depends(get_db)):
    data, errors = form.clean(creating=True)
    if errors:
        return render(request, "pharmacy/drug_form.html", status_code=422, drug=None, form=form, errors=errors, forms=DRUG_FORMS)
    opening = data.pop("opening_stock")
    drug = Drug(**data, stock_quantity=0)
    db.add(drug)
    db.flush()
    if opening:
        _move(db, drug, opening, "restock", "Opening stock", user)
    audit(db, request, user, "drug.created", "drug", drug.id, drug.label)
    db.commit()
    flash(request, f"{drug.name} added to the formulary.")
    return redirect(f"/pharmacy/drugs/{drug.id}")


@router.get("/drugs/{drug_id}")
def drug_detail(request: Request, drug_id: int, user: User = Depends(require("pharmacy.inventory")),
                db: Session = Depends(get_db)):
    drug = get_or_404(db, Drug, drug_id)
    form = DrugForm(**{k: "" if getattr(drug, k, None) is None else str(getattr(drug, k)) for k in
                       ("name", "generic_name", "form", "strength", "unit_price", "reorder_level", "expiry_date")},
                    is_active="on" if drug.is_active else "")
    moves = db.scalars(select(StockMovement).options(joinedload(StockMovement.user))
                       .where(StockMovement.drug_id == drug.id).order_by(StockMovement.id.desc()).limit(30)).all()
    return render(request, "pharmacy/drug_form.html", drug=drug, form=form, errors={}, forms=DRUG_FORMS, moves=moves)


@router.post("/drugs/{drug_id}")
def update_drug(request: Request, drug_id: int, form: Annotated[DrugForm, Form()],
                user: User = Depends(require("pharmacy.inventory")), db: Session = Depends(get_db)):
    drug = get_or_404(db, Drug, drug_id)
    data, errors = form.clean(creating=False)
    if errors:
        return render(request, "pharmacy/drug_form.html", status_code=422, drug=drug, form=form, errors=errors,
                      forms=DRUG_FORMS, moves=[])
    for k, v in data.items():
        setattr(drug, k, v)
    audit(db, request, user, "drug.updated", "drug", drug.id)
    db.commit()
    flash(request, "Drug details saved.")
    return redirect(f"/pharmacy/drugs/{drug.id}")


@router.post("/drugs/{drug_id}/stock")
def adjust_stock(request: Request, drug_id: int, kind: Annotated[str, Form()] = "restock",
                 quantity: Annotated[str, Form()] = "", reference: Annotated[str, Form()] = "",
                 expiry_date: Annotated[str, Form()] = "", user: User = Depends(require("pharmacy.inventory")),
                 db: Session = Depends(get_db)):
    drug = db.scalar(select(Drug).where(Drug.id == drug_id).with_for_update())
    if not drug:
        raise HTTPException(404)
    try:
        qty = parse_int(quantity, minimum=1)
        expiry = parse_date(expiry_date)
    except ValueError:
        qty = None
    if not qty or kind not in ("restock", "write_off"):
        flash(request, "Enter a quantity of 1 or more.", "error")
        return redirect(f"/pharmacy/drugs/{drug.id}#stock")
    if kind == "write_off":
        if qty > drug.stock_quantity:
            flash(request, f"Only {drug.stock_quantity} in stock.", "error")
            return redirect(f"/pharmacy/drugs/{drug.id}#stock")
        if not reference.strip():
            flash(request, "Give a reason for the write-off (expired, damaged…).", "error")
            return redirect(f"/pharmacy/drugs/{drug.id}#stock")
        _move(db, drug, -qty, "write_off", reference.strip(), user)
    else:
        _move(db, drug, qty, "restock", blank(reference), user)
        if expiry:
            drug.expiry_date = expiry
    audit(db, request, user, f"drug.{kind}", "drug", drug.id, f"{qty}")
    db.commit()
    flash(request, f"Stock updated — {drug.stock_quantity} on hand.")
    return redirect(f"/pharmacy/drugs/{drug.id}#stock")
