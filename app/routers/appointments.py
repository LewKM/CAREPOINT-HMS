from datetime import time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import (
    Appointment, AppointmentStatus, ChargeCategory, Department, Encounter, EncounterStatus, Patient, Role, User,
    VisitType, utcnow,
)
from ..security import require
from ..services.audit import audit
from ..services.billing import charge
from ..templating import back, flash, redirect, render
from ..utils import day_bounds, get_or_404, local_to_utc, parse_date, parse_int, today_local

router = APIRouter(prefix="/appointments")


def active_doctors(db: Session):
    return db.scalars(select(User).options(joinedload(User.department))
                      .where(User.role == Role.DOCTOR, User.is_active).order_by(User.full_name)).all()


def open_visit(db: Session, patient_id: int) -> Encounter | None:
    return db.scalar(select(Encounter).where(Encounter.patient_id == patient_id,
                                             Encounter.status != EncounterStatus.COMPLETED).limit(1))


def start_visit(db: Session, *, patient: Patient, department: Department, doctor_id: int | None, user: User,
                visit_type=VisitType.OUTPATIENT, priority=None, complaint=None, appointment=None) -> Encounter:
    """Open an encounter and bill the department's consultation fee onto its invoice."""
    enc = Encounter(patient_id=patient.id, doctor_id=doctor_id, department_id=department.id, visit_type=visit_type,
                    chief_complaint=complaint, appointment=appointment, created_by_id=user.id, checked_in_at=utcnow())
    if priority:
        enc.priority = priority
    db.add(enc)
    db.flush()
    if department.consultation_fee and department.consultation_fee > 0:
        charge(db, patient_id=patient.id, encounter_id=enc.id, category=ChargeCategory.CONSULTATION,
               description=f"Consultation — {department.name}", unit_price=department.consultation_fee, user_id=user.id)
    return enc


@router.get("")
def index(request: Request, day: str = "", doctor_id: str = "", status: str = "",
          user: User = Depends(require("appointments.view")), db: Session = Depends(get_db)):
    try:
        the_day = parse_date(day) or today_local()
    except ValueError:
        the_day = today_local()
    start, end = day_bounds(the_day)
    stmt = (select(Appointment).options(joinedload(Appointment.patient), joinedload(Appointment.doctor),
                                        joinedload(Appointment.department), joinedload(Appointment.encounter))
            .where(Appointment.scheduled_at >= start, Appointment.scheduled_at < end).order_by(Appointment.scheduled_at))
    # Doctors land on their own list; they can still pick "All doctors".
    if doctor_id == "" and user.role == Role.DOCTOR and "doctor_id" not in request.query_params:
        doctor_id = str(user.id)
    if doctor_id.isdigit():
        stmt = stmt.where(Appointment.doctor_id == int(doctor_id))
    if status in AppointmentStatus._value2member_map_:
        stmt = stmt.where(Appointment.status == status)
    return render(request, "appointments/index.html", appointments=db.scalars(stmt).all(), day=the_day,
                  prev_day=the_day - timedelta(days=1), next_day=the_day + timedelta(days=1),
                  doctors=active_doctors(db), doctor_id=doctor_id, status=status)


@router.get("/new")
def new(request: Request, patient_id: int | None = None, user: User = Depends(require("appointments.manage")),
        db: Session = Depends(get_db)):
    patient = db.get(Patient, patient_id) if patient_id else None
    return render(request, "appointments/form.html", patient=patient, doctors=active_doctors(db), errors={},
                  values={"day": today_local().isoformat(), "duration": "20"})


@router.post("/new")
def create(request: Request, patient_id: Annotated[str, Form()] = "", doctor_id: Annotated[str, Form()] = "",
           day: Annotated[str, Form()] = "", at: Annotated[str, Form()] = "", duration: Annotated[str, Form()] = "20",
           reason: Annotated[str, Form()] = "", user: User = Depends(require("appointments.manage")),
           db: Session = Depends(get_db)):
    errors = {}
    patient = db.get(Patient, int(patient_id)) if patient_id.isdigit() else None
    doctor = db.get(User, int(doctor_id)) if doctor_id.isdigit() else None
    if not patient:
        errors["patient_id"] = "Pick a registered patient."
    if not doctor or doctor.role != Role.DOCTOR or not doctor.is_active:
        errors["doctor_id"] = "Pick a doctor."
    elif not doctor.department_id:
        errors["doctor_id"] = "This doctor has no department yet — ask the administrator to assign one."
    try:
        the_day = parse_date(day)
        the_time = time.fromisoformat(at) if at else None
        mins = parse_int(duration, minimum=5, maximum=240) or 20
        if not the_day or not the_time:
            errors["when"] = "Choose a date and time."
        else:
            when = local_to_utc(the_day, the_time)
            if when < utcnow() - timedelta(minutes=5):
                errors["when"] = "That time has already passed."
    except ValueError:
        errors["when"] = "Enter a valid date, time and duration."

    if not errors:
        clash_start, clash_end = when - timedelta(minutes=240), when + timedelta(minutes=mins)
        candidates = db.scalars(select(Appointment).where(
            Appointment.doctor_id == doctor.id, Appointment.scheduled_at > clash_start, Appointment.scheduled_at < clash_end,
            Appointment.status.in_([AppointmentStatus.SCHEDULED, AppointmentStatus.CHECKED_IN]))).all()
        if any(a.scheduled_at + timedelta(minutes=a.duration_minutes) > when for a in candidates):
            errors["when"] = f"{doctor.display_name} already has an appointment overlapping that slot."

    if errors:
        return render(request, "appointments/form.html", status_code=422, patient=patient, doctors=active_doctors(db),
                      errors=errors, values={"doctor_id": doctor_id, "day": day, "at": at, "duration": duration, "reason": reason})

    appt = Appointment(patient_id=patient.id, doctor_id=doctor.id, department_id=doctor.department_id, scheduled_at=when,
                       duration_minutes=mins, reason=reason.strip() or None, created_by_id=user.id)
    db.add(appt)
    db.flush()
    audit(db, request, user, "appointment.booked", "appointment", appt.id, f"{patient.mrn} with {doctor.full_name}")
    db.commit()
    flash(request, f"Appointment booked for {patient.full_name} with {doctor.display_name}.")
    return redirect(f"/appointments?day={the_day.isoformat()}")


def _scheduled(db: Session, appointment_id: int) -> Appointment:
    appt = get_or_404(db, Appointment, appointment_id)
    if appt.status != AppointmentStatus.SCHEDULED:
        raise HTTPException(409, f"This appointment is already {appt.status.label.lower()}.")
    return appt


@router.post("/{appointment_id}/check-in")
def check_in(request: Request, appointment_id: int, user: User = Depends(require("visits.checkin")),
             db: Session = Depends(get_db)):
    appt = _scheduled(db, appointment_id)
    if open_visit(db, appt.patient_id):
        flash(request, f"{appt.patient.full_name} already has an open visit — finish that first.", "error")
        return redirect(f"/appointments?day={today_local().isoformat()}")
    enc = start_visit(db, patient=appt.patient, department=appt.department, doctor_id=appt.doctor_id, user=user,
                      complaint=appt.reason, appointment=appt)
    appt.status = AppointmentStatus.CHECKED_IN
    audit(db, request, user, "appointment.checked_in", "encounter", enc.id, appt.patient.mrn)
    db.commit()
    flash(request, f"{appt.patient.full_name} checked in and sent to triage.")
    return back(request, "/appointments")


@router.post("/{appointment_id}/cancel")
def cancel(request: Request, appointment_id: int, user: User = Depends(require("appointments.manage")),
           db: Session = Depends(get_db)):
    appt = _scheduled(db, appointment_id)
    appt.status = AppointmentStatus.CANCELLED
    audit(db, request, user, "appointment.cancelled", "appointment", appt.id)
    db.commit()
    flash(request, "Appointment cancelled.")
    return back(request, "/appointments")


@router.post("/{appointment_id}/no-show")
def no_show(request: Request, appointment_id: int, user: User = Depends(require("appointments.manage")),
            db: Session = Depends(get_db)):
    appt = _scheduled(db, appointment_id)
    appt.status = AppointmentStatus.NO_SHOW
    audit(db, request, user, "appointment.no_show", "appointment", appt.id)
    db.commit()
    flash(request, "Marked as no-show.")
    return back(request, "/appointments")
