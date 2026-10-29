from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import (
    PRIORITY_RANK, Admission, AdmissionStatus, AppointmentStatus, Bed, BedStatus, ChargeCategory, Department, Drug,
    Encounter, EncounterStatus, LabOrder, LabPriority, LabTest, Patient, Prescription, PrescriptionItem,
    PrescriptionStatus, Priority, Role, User, Vitals, VisitType, Ward, utcnow,
)
from ..permissions import can
from ..security import require
from ..services.audit import audit
from ..services.billing import charge
from ..services.documents import document
from ..templating import back, flash, redirect, render
from ..utils import blank, day_bounds, get_or_404, parse_date, parse_decimal, parse_int
from .appointments import open_visit, start_visit

router = APIRouter(prefix="/visits")

ACTIVE = (EncounterStatus.WAITING, EncounterStatus.TRIAGED, EncounterStatus.IN_PROGRESS)


def _active_encounter(db: Session, encounter_id: int) -> Encounter:
    enc = get_or_404(db, Encounter, encounter_id)
    if enc.status == EncounterStatus.COMPLETED:
        raise HTTPException(409, "This visit has been closed and can no longer be changed.")
    return enc


def _ensure_started(enc: Encounter, user: User) -> None:
    """The first clinical action by a doctor takes ownership of the visit."""
    if enc.status != EncounterStatus.IN_PROGRESS:
        enc.status = EncounterStatus.IN_PROGRESS
        enc.started_at = enc.started_at or utcnow()
    if user.role == Role.DOCTOR and enc.doctor_id != user.id:
        enc.doctor_id = user.id


@router.get("")
def index(request: Request, status: str = "active", mine: str = "", user: User = Depends(require("visits.view")),
          db: Session = Depends(get_db)):
    stmt = select(Encounter).options(joinedload(Encounter.patient), joinedload(Encounter.doctor), joinedload(Encounter.department))
    if status == "completed":
        start, end = day_bounds()
        stmt = stmt.where(Encounter.completed_at >= start, Encounter.completed_at < end)
    elif status in EncounterStatus._value2member_map_:
        stmt = stmt.where(Encounter.status == status)
    else:
        status = "active"
        stmt = stmt.where(Encounter.status.in_(ACTIVE))
    if mine and user.role == Role.DOCTOR:
        stmt = stmt.where((Encounter.doctor_id == user.id) | Encounter.doctor_id.is_(None))
    visits = db.scalars(stmt).all()
    if status == "completed":
        visits = sorted(visits, key=lambda e: e.completed_at, reverse=True)
    else:
        visits = sorted(visits, key=lambda e: (PRIORITY_RANK[e.priority], e.checked_in_at))
    return render(request, "visits/index.html", visits=visits, status=status, mine=mine)


@router.get("/new")
def new(request: Request, patient_id: int | None = None, user: User = Depends(require("visits.checkin")),
        db: Session = Depends(get_db)):
    return render(request, "visits/form.html", patient=db.get(Patient, patient_id) if patient_id else None,
                  departments=_clinical_departments(db), doctors=_doctors(db), errors={}, values={})


def _clinical_departments(db):
    return db.scalars(select(Department).where(Department.is_clinical).order_by(Department.name)).all()


def _doctors(db):
    return db.scalars(select(User).where(User.role == Role.DOCTOR, User.is_active).order_by(User.full_name)).all()


@router.post("/new")
def create(request: Request, patient_id: Annotated[str, Form()] = "", department_id: Annotated[str, Form()] = "",
           doctor_id: Annotated[str, Form()] = "", visit_type: Annotated[str, Form()] = "outpatient",
           priority: Annotated[str, Form()] = "routine", complaint: Annotated[str, Form()] = "",
           user: User = Depends(require("visits.checkin")), db: Session = Depends(get_db)):
    errors = {}
    patient = db.get(Patient, int(patient_id)) if patient_id.isdigit() else None
    dept = db.get(Department, int(department_id)) if department_id.isdigit() else None
    doctor = db.get(User, int(doctor_id)) if doctor_id.isdigit() else None
    if not patient:
        errors["patient_id"] = "Pick a registered patient."
    elif open_visit(db, patient.id):
        errors["patient_id"] = "This patient already has an open visit."
    if not dept or not dept.is_clinical:
        errors["department_id"] = "Pick a department."
    if doctor_id and (not doctor or doctor.role != Role.DOCTOR):
        errors["doctor_id"] = "Pick a doctor or leave it for the next available one."
    if visit_type not in VisitType._value2member_map_ or priority not in Priority._value2member_map_:
        errors["visit_type"] = "Choose a visit type and priority."
    if errors:
        return render(request, "visits/form.html", status_code=422, patient=patient, departments=_clinical_departments(db),
                      doctors=_doctors(db), errors=errors,
                      values=dict(department_id=department_id, doctor_id=doctor_id, visit_type=visit_type,
                                  priority=priority, complaint=complaint))
    enc = start_visit(db, patient=patient, department=dept, doctor_id=doctor.id if doctor else None, user=user,
                      visit_type=VisitType(visit_type), priority=Priority(priority), complaint=blank(complaint))
    audit(db, request, user, "visit.walk_in", "encounter", enc.id, f"{patient.mrn} {visit_type}/{priority}")
    db.commit()
    flash(request, f"{patient.full_name} added to the {'emergency ' if visit_type == 'emergency' else ''}queue.")
    return redirect("/visits")


def _full_record(db: Session, encounter_id: int) -> Encounter:
    """The visit with everything a clinician reads, loaded in one query."""
    enc = db.scalar(select(Encounter).options(
        joinedload(Encounter.patient), joinedload(Encounter.doctor), joinedload(Encounter.department),
        joinedload(Encounter.vitals).joinedload(Vitals.recorded_by),
        joinedload(Encounter.prescriptions).joinedload(Prescription.items).joinedload(PrescriptionItem.drug),
        joinedload(Encounter.lab_orders).joinedload(LabOrder.test),
        joinedload(Encounter.lab_orders).joinedload(LabOrder.performed_by),
        joinedload(Encounter.admission)).where(Encounter.id == encounter_id))
    if not enc:
        raise HTTPException(404)
    return enc


@router.get("/{encounter_id}")
def show(request: Request, encounter_id: int, user: User = Depends(require("patients.clinical")),
         db: Session = Depends(get_db)):
    enc = _full_record(db, encounter_id)
    ctx = dict(enc=enc, patient=enc.patient)
    if can(user, "prescriptions.write"):
        ctx["drugs"] = db.scalars(select(Drug).where(Drug.is_active).order_by(Drug.name)).all()
    if can(user, "lab.order"):
        ctx["tests"] = db.scalars(select(LabTest).where(LabTest.is_active).order_by(LabTest.category, LabTest.name)).all()
    if can(user, "wards.admit") and not enc.admission:
        ctx["beds"] = db.scalars(select(Bed).join(Ward).options(joinedload(Bed.ward))
                                 .where(Bed.status == BedStatus.AVAILABLE).order_by(Ward.name, Bed.label)).all()
    audit(db, request, user, "visit.viewed", "encounter", enc.id)
    db.commit()
    return render(request, "visits/show.html", **ctx)


@router.get("/{encounter_id}/summary")
def summary(request: Request, encounter_id: int, user: User = Depends(require("patients.clinical")),
            db: Session = Depends(get_db)):
    enc = _full_record(db, encounter_id)
    audit(db, request, user, "visit.printed", "encounter", enc.id, "visit summary")
    db.commit()
    return document(request, "print/visit_summary.html", f"Visit-summary-{enc.patient.mrn}-{enc.id}", enc=enc, patient=enc.patient)


@router.get("/{encounter_id}/lab-report")
def lab_report(request: Request, encounter_id: int, order: int | None = None,
               user: User = Depends(require("patients.clinical", "lab.view")), db: Session = Depends(get_db)):
    enc = _full_record(db, encounter_id)
    audit(db, request, user, "visit.printed", "encounter", enc.id, "lab report")
    db.commit()
    back_to = f"/lab/orders/{order}" if order and not can(user, "patients.clinical") else f"/visits/{enc.id}#labs"
    return document(request, "print/lab_report.html", f"Lab-report-{enc.patient.mrn}-{enc.id}",
                    enc=enc, patient=enc.patient, back_to=back_to)


@router.post("/{encounter_id}/vitals")
def record_vitals(request: Request, encounter_id: int, temperature: Annotated[str, Form()] = "",
                  systolic: Annotated[str, Form()] = "", diastolic: Annotated[str, Form()] = "",
                  pulse: Annotated[str, Form()] = "", respiratory_rate: Annotated[str, Form()] = "",
                  spo2: Annotated[str, Form()] = "", weight_kg: Annotated[str, Form()] = "",
                  height_cm: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
                  priority: Annotated[str, Form()] = "", user: User = Depends(require("triage.record")),
                  db: Session = Depends(get_db)):
    enc = _active_encounter(db, encounter_id)
    try:
        v = Vitals(
            encounter_id=enc.id, patient_id=enc.patient_id, recorded_by_id=user.id, recorded_at=utcnow(),
            temperature=parse_decimal(temperature), systolic=parse_int(systolic, minimum=40, maximum=300),
            diastolic=parse_int(diastolic, minimum=20, maximum=200), pulse=parse_int(pulse, minimum=20, maximum=250),
            respiratory_rate=parse_int(respiratory_rate, minimum=4, maximum=80), spo2=parse_int(spo2, minimum=40, maximum=100),
            weight_kg=parse_decimal(weight_kg), height_cm=parse_decimal(height_cm), notes=blank(notes),
        )
    except ValueError as e:
        flash(request, f"Vitals not saved: {e}.", "error")
        return redirect(f"/visits/{enc.id}#vitals")
    if v.temperature is not None and not (30 <= v.temperature <= 45):
        flash(request, "Vitals not saved: temperature must be between 30 and 45 °C.", "error")
        return redirect(f"/visits/{enc.id}#vitals")
    if all(getattr(v, f) is None for f in ("temperature", "systolic", "pulse", "respiratory_rate", "spo2", "weight_kg")):
        flash(request, "Enter at least one reading.", "error")
        return redirect(f"/visits/{enc.id}#vitals")
    db.add(v)
    if priority in Priority._value2member_map_:
        enc.priority = Priority(priority)
    if enc.status == EncounterStatus.WAITING:
        enc.status = EncounterStatus.TRIAGED
    audit(db, request, user, "visit.vitals", "encounter", enc.id)
    db.commit()
    flash(request, "Vitals recorded." + (" Abnormal readings flagged." if v.flags else ""))
    return redirect(f"/visits/{enc.id}#vitals")


@router.post("/{encounter_id}/start")
def start(request: Request, encounter_id: int, user: User = Depends(require("consult.perform")),
          db: Session = Depends(get_db)):
    enc = _active_encounter(db, encounter_id)
    _ensure_started(enc, user)
    audit(db, request, user, "visit.started", "encounter", enc.id)
    db.commit()
    return redirect(f"/visits/{enc.id}#consultation")


@router.post("/{encounter_id}/notes")
def save_notes(request: Request, encounter_id: int, chief_complaint: Annotated[str, Form()] = "",
               history: Annotated[str, Form()] = "", examination: Annotated[str, Form()] = "",
               diagnosis: Annotated[str, Form()] = "", treatment_plan: Annotated[str, Form()] = "",
               follow_up_date: Annotated[str, Form()] = "", user: User = Depends(require("consult.perform")),
               db: Session = Depends(get_db)):
    enc = _active_encounter(db, encounter_id)
    try:
        enc.follow_up_date = parse_date(follow_up_date)
    except ValueError:
        flash(request, "Follow-up date is not valid.", "error")
        return redirect(f"/visits/{enc.id}#consultation")
    _ensure_started(enc, user)
    enc.chief_complaint, enc.history, enc.examination = blank(chief_complaint), blank(history), blank(examination)
    enc.diagnosis, enc.treatment_plan = blank(diagnosis), blank(treatment_plan)
    audit(db, request, user, "visit.notes", "encounter", enc.id)
    db.commit()
    flash(request, "Consultation notes saved.")
    return redirect(f"/visits/{enc.id}#consultation")


@router.post("/{encounter_id}/prescriptions")
def prescribe(request: Request, encounter_id: int, drug_id: Annotated[list[str], Form()] = [],
              dose: Annotated[list[str], Form()] = [], frequency: Annotated[list[str], Form()] = [],
              duration_days: Annotated[list[str], Form()] = [], quantity: Annotated[list[str], Form()] = [],
              instructions: Annotated[list[str], Form()] = [], notes: Annotated[str, Form()] = "",
              user: User = Depends(require("prescriptions.write")), db: Session = Depends(get_db)):
    enc = _active_encounter(db, encounter_id)
    rows = list(zip(drug_id, dose, frequency, duration_days, quantity, instructions))
    items = []
    try:
        for d_id, d_dose, d_freq, d_days, d_qty, d_instr in rows:
            if not d_id:
                continue
            drug = db.get(Drug, int(d_id))
            if not drug or not drug.is_active:
                raise ValueError("an unknown drug was selected")
            if not d_dose.strip() or not d_freq.strip():
                raise ValueError(f"dose and frequency are required for {drug.name}")
            items.append(PrescriptionItem(
                drug_id=drug.id, dose=d_dose.strip(), frequency=d_freq.strip(),
                duration_days=parse_int(d_days, minimum=1, maximum=365) or 1,
                quantity=parse_int(d_qty, minimum=1, maximum=10000) or 1, instructions=blank(d_instr)))
    except ValueError as e:
        flash(request, f"Prescription not saved: {e}.", "error")
        return redirect(f"/visits/{enc.id}#prescriptions")
    if not items:
        flash(request, "Add at least one drug.", "error")
        return redirect(f"/visits/{enc.id}#prescriptions")
    _ensure_started(enc, user)
    rx = Prescription(encounter_id=enc.id, patient_id=enc.patient_id, doctor_id=user.id, notes=blank(notes),
                      items=items, created_at=utcnow())
    db.add(rx)
    db.flush()
    audit(db, request, user, "prescription.created", "prescription", rx.id, f"{len(items)} item(s)")
    db.commit()
    flash(request, "Prescription sent to pharmacy.")
    return redirect(f"/visits/{enc.id}#prescriptions")


@router.post("/{encounter_id}/prescriptions/{rx_id}/cancel")
def cancel_prescription(request: Request, encounter_id: int, rx_id: int,
                        user: User = Depends(require("prescriptions.write")), db: Session = Depends(get_db)):
    rx = get_or_404(db, Prescription, rx_id)
    if rx.encounter_id != encounter_id or rx.status != PrescriptionStatus.PENDING:
        raise HTTPException(409, "Only prescriptions still pending at the pharmacy can be cancelled.")
    rx.status, rx.cancel_reason = PrescriptionStatus.CANCELLED, f"Cancelled by {user.display_name}"
    audit(db, request, user, "prescription.cancelled", "prescription", rx.id)
    db.commit()
    flash(request, "Prescription cancelled.")
    return redirect(f"/visits/{encounter_id}#prescriptions")


@router.post("/{encounter_id}/lab-orders")
def order_labs(request: Request, encounter_id: int, test_id: Annotated[list[str], Form()] = [],
               priority: Annotated[str, Form()] = "routine", clinical_notes: Annotated[str, Form()] = "",
               user: User = Depends(require("lab.order")), db: Session = Depends(get_db)):
    enc = _active_encounter(db, encounter_id)
    tests = [t for t in (db.get(LabTest, int(i)) for i in test_id if i.isdigit()) if t and t.is_active]
    if not tests:
        flash(request, "Select at least one test.", "error")
        return redirect(f"/visits/{enc.id}#labs")
    prio = LabPriority(priority) if priority in LabPriority._value2member_map_ else LabPriority.ROUTINE
    _ensure_started(enc, user)
    for t in tests:
        db.add(LabOrder(encounter_id=enc.id, patient_id=enc.patient_id, test_id=t.id, ordered_by_id=user.id,
                        priority=prio, clinical_notes=blank(clinical_notes), created_at=utcnow()))
        charge(db, patient_id=enc.patient_id, encounter_id=enc.id, category=ChargeCategory.LABORATORY,
               description=f"Lab — {t.name}", unit_price=t.price, user_id=user.id)
    audit(db, request, user, "lab.ordered", "encounter", enc.id, ", ".join(t.code for t in tests))
    db.commit()
    flash(request, f"{len(tests)} lab test(s) ordered.")
    return redirect(f"/visits/{enc.id}#labs")


@router.post("/{encounter_id}/admit")
def admit(request: Request, encounter_id: int, bed_id: Annotated[str, Form()] = "", reason: Annotated[str, Form()] = "",
          user: User = Depends(require("wards.admit")), db: Session = Depends(get_db)):
    enc = _active_encounter(db, encounter_id)
    if enc.admission:
        raise HTTPException(409, "This patient is already admitted from this visit.")
    bed = db.scalar(select(Bed).where(Bed.id == int(bed_id)).with_for_update()) if bed_id.isdigit() else None
    if not bed or bed.status != BedStatus.AVAILABLE:
        flash(request, "That bed is no longer available — pick another.", "error")
        return redirect(f"/visits/{enc.id}#admission")
    if not reason.strip():
        flash(request, "Give a reason for admission.", "error")
        return redirect(f"/visits/{enc.id}#admission")
    _ensure_started(enc, user)
    adm = Admission(patient_id=enc.patient_id, encounter_id=enc.id, bed_id=bed.id, admitting_doctor_id=user.id,
                    reason=reason.strip(), admitted_at=utcnow())
    bed.status = BedStatus.OCCUPIED
    db.add(adm)
    db.flush()
    audit(db, request, user, "patient.admitted", "admission", adm.id, f"{enc.patient.mrn} → {bed.ward.name} {bed.label}")
    db.commit()
    flash(request, f"Admitted to {bed.ward.name}, bed {bed.label}.")
    return redirect(f"/admissions/{adm.id}")


@router.post("/{encounter_id}/complete")
def complete(request: Request, encounter_id: int, user: User = Depends(require("consult.perform")),
             db: Session = Depends(get_db)):
    enc = _active_encounter(db, encounter_id)
    if enc.admission and enc.admission.status == AdmissionStatus.ADMITTED:
        flash(request, "The patient is admitted — the visit closes when they are discharged from the ward.", "error")
        return redirect(f"/visits/{enc.id}")
    if not enc.diagnosis:
        flash(request, "Record a diagnosis before closing the visit.", "error")
        return redirect(f"/visits/{enc.id}#consultation")
    close_encounter(enc, user)
    audit(db, request, user, "visit.completed", "encounter", enc.id)
    db.commit()
    flash(request, f"Visit for {enc.patient.full_name} closed.")
    return redirect("/visits")


def close_encounter(enc: Encounter, user: User) -> None:
    enc.status = EncounterStatus.COMPLETED
    enc.completed_at = utcnow()
    if enc.doctor_id is None and user.role == Role.DOCTOR:
        enc.doctor_id = user.id
    if enc.appointment and enc.appointment.status == AppointmentStatus.CHECKED_IN:
        enc.appointment.status = AppointmentStatus.COMPLETED
