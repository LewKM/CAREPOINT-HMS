from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import (
    Admission, Appointment, Encounter, Gender, Invoice, LabOrder, Patient, Prescription, User, Vitals,
)
from ..permissions import can
from ..security import require
from ..services.audit import audit
from ..templating import flash, redirect, render
from ..utils import blank, get_or_404, paginate, parse_date

router = APIRouter(prefix="/patients")

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]


class PatientForm(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    first_name: str = ""
    last_name: str = ""
    gender: str = ""
    date_of_birth: str = ""
    phone: str = ""
    email: str = ""
    national_id: str = ""
    address: str = ""
    blood_group: str = ""
    allergies: str = ""
    chronic_conditions: str = ""
    next_of_kin_name: str = ""
    next_of_kin_phone: str = ""
    next_of_kin_relation: str = ""
    insurance_provider: str = ""
    insurance_number: str = ""

    def clean(self) -> tuple[dict, dict]:
        errors, data = {}, {}
        if not self.first_name:
            errors["first_name"] = "Required."
        if not self.last_name:
            errors["last_name"] = "Required."
        if self.gender not in Gender._value2member_map_:
            errors["gender"] = "Choose a gender."
        try:
            dob = parse_date(self.date_of_birth)
            if dob is None:
                errors["date_of_birth"] = "Required."
            elif dob > date.today() or dob.year < 1900:
                errors["date_of_birth"] = "Enter a real date of birth."
            data["date_of_birth"] = dob
        except ValueError:
            errors["date_of_birth"] = "Enter a valid date."
        if self.blood_group and self.blood_group not in BLOOD_GROUPS:
            errors["blood_group"] = "Unknown blood group."
        if self.email and "@" not in self.email:
            errors["email"] = "Enter a valid email."
        for field in ("first_name", "last_name", "phone", "email", "national_id", "address", "blood_group",
                      "allergies", "chronic_conditions", "next_of_kin_name", "next_of_kin_phone",
                      "next_of_kin_relation", "insurance_provider", "insurance_number"):
            data[field] = blank(getattr(self, field))
        data["first_name"] = (data["first_name"] or "").title() or None
        data["last_name"] = (data["last_name"] or "").title() or None
        data["gender"] = self.gender or None
        return data, errors


def search_stmt(q: str):
    stmt = select(Patient)
    for term in q.split():
        like = f"%{term}%"
        stmt = stmt.where(or_(Patient.first_name.ilike(like), Patient.last_name.ilike(like), Patient.mrn.ilike(like),
                              Patient.phone.ilike(like), Patient.national_id.ilike(like)))
    return stmt


@router.get("")
def index(request: Request, q: str = "", page: int = 1, user: User = Depends(require("patients.view")),
          db: Session = Depends(get_db)):
    stmt = search_stmt(q.strip()).order_by(Patient.id.desc())
    return render(request, "patients/index.html", page=paginate(db, stmt, page), q=q)


@router.get("/lookup")
def lookup(q: str = "", user: User = Depends(require("patients.view")), db: Session = Depends(get_db)):
    if len(q.strip()) < 2:
        return []
    rows = db.scalars(search_stmt(q.strip()).order_by(Patient.last_name).limit(12)).all()
    return [{"id": p.id, "mrn": p.mrn, "name": p.full_name, "age": p.age, "gender": p.gender.label,
             "phone": p.phone or ""} for p in rows]


@router.get("/new")
def new(request: Request, user: User = Depends(require("patients.register"))):
    return render(request, "patients/form.html", form=PatientForm(), errors={}, patient=None, blood_groups=BLOOD_GROUPS)


@router.post("/new")
def create(request: Request, form: Annotated[PatientForm, Form()], user: User = Depends(require("patients.register")),
           db: Session = Depends(get_db)):
    data, errors = form.clean()
    if data.get("national_id") and db.scalar(select(Patient.id).where(Patient.national_id == data["national_id"])):
        errors["national_id"] = "A patient with this ID number is already registered."
    if errors:
        return render(request, "patients/form.html", status_code=422, form=form, errors=errors, patient=None,
                      blood_groups=BLOOD_GROUPS)
    patient = Patient(**data, registered_by_id=user.id)
    db.add(patient)
    db.flush()
    patient.mrn = f"CP{patient.id:06d}"
    audit(db, request, user, "patient.registered", "patient", patient.id, patient.mrn)
    db.commit()
    flash(request, f"{patient.full_name} registered with MRN {patient.mrn}.")
    return redirect(f"/patients/{patient.id}")


@router.get("/{patient_id}")
def show(request: Request, patient_id: int, user: User = Depends(require("patients.view")), db: Session = Depends(get_db)):
    patient = get_or_404(db, Patient, patient_id)
    ctx = dict(patient=patient)
    ctx["appointments"] = db.scalars(select(Appointment).options(joinedload(Appointment.doctor))
                                     .where(Appointment.patient_id == patient.id)
                                     .order_by(Appointment.scheduled_at.desc()).limit(10)).all()
    ctx["active_visit"] = db.scalar(select(Encounter).where(Encounter.patient_id == patient.id,
                                                            Encounter.completed_at.is_(None)).limit(1))
    ctx["admissions"] = db.scalars(select(Admission).options(joinedload(Admission.bed))
                                   .where(Admission.patient_id == patient.id).order_by(Admission.admitted_at.desc())).all()
    if can(user, "patients.clinical"):
        ctx["visits"] = db.scalars(select(Encounter).options(joinedload(Encounter.doctor), joinedload(Encounter.department))
                                   .where(Encounter.patient_id == patient.id).order_by(Encounter.checked_in_at.desc()).limit(20)).all()
        ctx["latest_vitals"] = db.scalar(select(Vitals).where(Vitals.patient_id == patient.id).order_by(Vitals.recorded_at.desc()).limit(1))
        ctx["labs"] = db.scalars(select(LabOrder).options(joinedload(LabOrder.test))
                                 .where(LabOrder.patient_id == patient.id).order_by(LabOrder.created_at.desc()).limit(15)).all()
        ctx["prescriptions"] = db.scalars(select(Prescription).options(joinedload(Prescription.items), joinedload(Prescription.doctor))
                                          .where(Prescription.patient_id == patient.id)
                                          .order_by(Prescription.created_at.desc()).limit(10)).unique().all()
    if can(user, "billing.view"):
        ctx["invoices"] = db.scalars(select(Invoice).where(Invoice.patient_id == patient.id).order_by(Invoice.id.desc())).all()
    audit(db, request, user, "patient.viewed", "patient", patient.id)
    db.commit()
    return render(request, "patients/show.html", **ctx)


@router.get("/{patient_id}/edit")
def edit(request: Request, patient_id: int, user: User = Depends(require("patients.edit")), db: Session = Depends(get_db)):
    patient = get_or_404(db, Patient, patient_id)
    values = {k: ("" if v is None else str(getattr(v, "value", v))) for k, v in
              ((f, getattr(patient, f)) for f in PatientForm.model_fields)}
    return render(request, "patients/form.html", form=PatientForm(**values), errors={}, patient=patient,
                  blood_groups=BLOOD_GROUPS)


@router.post("/{patient_id}/edit")
def update(request: Request, patient_id: int, form: Annotated[PatientForm, Form()],
           user: User = Depends(require("patients.edit")), db: Session = Depends(get_db)):
    patient = get_or_404(db, Patient, patient_id)
    data, errors = form.clean()
    if data.get("national_id") and db.scalar(select(Patient.id).where(Patient.national_id == data["national_id"],
                                                                      Patient.id != patient.id)):
        errors["national_id"] = "Another patient already has this ID number."
    if errors:
        return render(request, "patients/form.html", status_code=422, form=form, errors=errors, patient=patient,
                      blood_groups=BLOOD_GROUPS)
    changed = [k for k, v in data.items() if str(getattr(getattr(patient, k), "value", getattr(patient, k)) or "") != str(v or "")]
    for k, v in data.items():
        setattr(patient, k, v)
    audit(db, request, user, "patient.updated", "patient", patient.id, ", ".join(changed) or None)
    db.commit()
    flash(request, "Patient details saved.")
    return redirect(f"/patients/{patient.id}")
