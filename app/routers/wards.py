from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import (
    Admission, AdmissionStatus, Bed, BedStatus, ChargeCategory, ClinicalNote, Encounter, LabOrder, NoteType,
    Prescription, PrescriptionItem, Role, User, Ward, utcnow,
)
from ..permissions import can
from ..security import require
from ..services.audit import audit
from ..services.billing import charge
from ..templating import back, flash, redirect, render
from ..utils import blank, get_or_404, paginate
from .visits import close_encounter

router = APIRouter()

DISCHARGE_CONDITIONS = ["Recovered", "Improved", "Referred", "Against medical advice", "Deceased"]


@router.get("/wards")
def board(request: Request, user: User = Depends(require("wards.view")), db: Session = Depends(get_db)):
    wards = db.scalars(select(Ward).options(joinedload(Ward.beds)).order_by(Ward.name)).unique().all()
    occupants = {a.bed_id: a for a in db.scalars(
        select(Admission).options(joinedload(Admission.patient))
        .where(Admission.status == AdmissionStatus.ADMITTED)).all()}
    return render(request, "wards/board.html", wards=wards, occupants=occupants)


@router.post("/wards/beds/{bed_id}/status")
def set_bed_status(request: Request, bed_id: int, status: Annotated[str, Form()] = "",
                   user: User = Depends(require("wards.beds")), db: Session = Depends(get_db)):
    bed = get_or_404(db, Bed, bed_id)
    allowed = {BedStatus.AVAILABLE, BedStatus.CLEANING, BedStatus.MAINTENANCE}
    if bed.status == BedStatus.OCCUPIED:
        raise HTTPException(409, "An occupied bed is released by discharging or transferring the patient.")
    if status not in BedStatus._value2member_map_ or BedStatus(status) not in allowed:
        raise HTTPException(422, "Unknown bed status.")
    bed.status = BedStatus(status)
    audit(db, request, user, "bed.status", "bed", bed.id, f"{bed.ward.code}-{bed.label} → {status}")
    db.commit()
    flash(request, f"Bed {bed.label} marked {bed.status.label.lower()}.")
    return back(request, "/wards")


@router.get("/admissions")
def admissions(request: Request, status: str = "admitted", page: int = 1, user: User = Depends(require("wards.view")),
               db: Session = Depends(get_db)):
    stmt = select(Admission).options(joinedload(Admission.patient), joinedload(Admission.bed).joinedload(Bed.ward),
                                     joinedload(Admission.admitting_doctor))
    if status == "discharged":
        stmt = stmt.where(Admission.status == AdmissionStatus.DISCHARGED).order_by(Admission.discharged_at.desc())
    else:
        status = "admitted"
        stmt = stmt.where(Admission.status == AdmissionStatus.ADMITTED).order_by(Admission.admitted_at)
    return render(request, "wards/admissions.html", page=paginate(db, stmt, page), status=status)


@router.get("/admissions/{admission_id}")
def admission(request: Request, admission_id: int, user: User = Depends(require("wards.view")),
              db: Session = Depends(get_db)):
    adm = db.scalar(select(Admission).options(
        joinedload(Admission.patient), joinedload(Admission.bed).joinedload(Bed.ward), joinedload(Admission.admitting_doctor),
        joinedload(Admission.encounter), joinedload(Admission.notes).joinedload(ClinicalNote.author),
    ).where(Admission.id == admission_id))
    if not adm:
        raise HTTPException(404)
    ctx = dict(adm=adm, patient=adm.patient, conditions=DISCHARGE_CONDITIONS)
    if can(user, "wards.transfer") and adm.status == AdmissionStatus.ADMITTED:
        ctx["free_beds"] = db.scalars(select(Bed).join(Ward).options(joinedload(Bed.ward))
                                      .where(Bed.status == BedStatus.AVAILABLE).order_by(Ward.name, Bed.label)).all()
    audit(db, request, user, "admission.viewed", "admission", adm.id)
    db.commit()
    return render(request, "wards/admission.html", **ctx)


@router.get("/admissions/{admission_id}/discharge-summary")
def discharge_summary(request: Request, admission_id: int, user: User = Depends(require("patients.clinical")),
                      db: Session = Depends(get_db)):
    enc = joinedload(Admission.encounter)
    adm = db.scalar(select(Admission).options(
        joinedload(Admission.patient), joinedload(Admission.bed).joinedload(Bed.ward),
        joinedload(Admission.admitting_doctor), joinedload(Admission.discharged_by),
        enc.joinedload(Encounter.prescriptions).joinedload(Prescription.items).joinedload(PrescriptionItem.drug),
        enc.joinedload(Encounter.lab_orders).joinedload(LabOrder.test),
        enc.joinedload(Encounter.lab_orders).joinedload(LabOrder.performed_by),
    ).where(Admission.id == admission_id))
    if not adm:
        raise HTTPException(404)
    if adm.status == AdmissionStatus.ADMITTED:
        raise HTTPException(409, "The discharge summary is available once the patient has been discharged.")
    audit(db, request, user, "admission.printed", "admission", adm.id, "discharge summary")
    db.commit()
    return render(request, "print/discharge_summary.html", adm=adm, patient=adm.patient)


def _current(db: Session, admission_id: int) -> Admission:
    adm = get_or_404(db, Admission, admission_id)
    if adm.status != AdmissionStatus.ADMITTED:
        raise HTTPException(409, "This patient has already been discharged.")
    return adm


@router.post("/admissions/{admission_id}/notes")
def add_note(request: Request, admission_id: int, body: Annotated[str, Form()] = "",
             user: User = Depends(require("wards.notes")), db: Session = Depends(get_db)):
    adm = _current(db, admission_id)
    if not body.strip():
        flash(request, "The note is empty.", "error")
        return redirect(f"/admissions/{adm.id}")
    note_type = NoteType.PROGRESS if user.role == Role.DOCTOR else NoteType.NURSING
    db.add(ClinicalNote(admission_id=adm.id, author_id=user.id, note_type=note_type, body=body.strip(), created_at=utcnow()))
    audit(db, request, user, "admission.note", "admission", adm.id, note_type.value)
    db.commit()
    flash(request, f"{note_type.label} note added.")
    return redirect(f"/admissions/{adm.id}#notes")


@router.post("/admissions/{admission_id}/transfer")
def transfer(request: Request, admission_id: int, bed_id: Annotated[str, Form()] = "",
             user: User = Depends(require("wards.transfer")), db: Session = Depends(get_db)):
    adm = _current(db, admission_id)
    new_bed = db.scalar(select(Bed).where(Bed.id == int(bed_id)).with_for_update()) if bed_id.isdigit() else None
    if not new_bed or new_bed.status != BedStatus.AVAILABLE:
        flash(request, "That bed is no longer available.", "error")
        return redirect(f"/admissions/{adm.id}")
    old = adm.bed
    # Bed-days already accrued are billed at the old ward's rate before the move.
    if old.ward_id != new_bed.ward_id:
        _bill_bed_days(db, adm, user)
        adm.billed_from = utcnow()
    old.status = BedStatus.CLEANING
    new_bed.status = BedStatus.OCCUPIED
    adm.bed_id = new_bed.id
    db.add(ClinicalNote(admission_id=adm.id, author_id=user.id, note_type=NoteType.NURSING, created_at=utcnow(),
                        body=f"Transferred from {old.ward.name} {old.label} to {new_bed.ward.name} {new_bed.label}."))
    audit(db, request, user, "admission.transfer", "admission", adm.id, f"{old.label} → {new_bed.label}")
    db.commit()
    flash(request, f"Moved to {new_bed.ward.name}, bed {new_bed.label}. Old bed set to cleaning.")
    return redirect(f"/admissions/{adm.id}")


def _bill_bed_days(db: Session, adm: Admission, user: User) -> None:
    ward = adm.bed.ward
    if ward.daily_rate > 0:
        charge(db, patient_id=adm.patient_id, encounter_id=adm.encounter_id, category=ChargeCategory.WARD,
               description=f"Ward — {ward.name} (bed-days)", unit_price=ward.daily_rate,
               quantity=adm.billable_days, user_id=user.id)


@router.post("/admissions/{admission_id}/discharge")
def discharge(request: Request, admission_id: int, condition: Annotated[str, Form()] = "",
              summary: Annotated[str, Form()] = "", user: User = Depends(require("wards.discharge")),
              db: Session = Depends(get_db)):
    adm = _current(db, admission_id)
    if condition not in DISCHARGE_CONDITIONS or not summary.strip():
        flash(request, "A discharge condition and summary are both required.", "error")
        return redirect(f"/admissions/{adm.id}#discharge")
    adm.discharged_at = utcnow()
    _bill_bed_days(db, adm, user)
    adm.status = AdmissionStatus.DISCHARGED
    adm.discharged_by_id = user.id
    adm.discharge_condition, adm.discharge_summary = condition, blank(summary)
    adm.bed.status = BedStatus.CLEANING
    enc = adm.encounter
    if enc.is_active:
        enc.diagnosis = enc.diagnosis or adm.discharge_summary
        close_encounter(enc, user)
    audit(db, request, user, "patient.discharged", "admission", adm.id, condition)
    db.commit()
    flash(request, f"{adm.patient.full_name} discharged. Bed {adm.bed.label} sent for cleaning; ward charges added to the bill.")
    return redirect(f"/admissions/{adm.id}")
