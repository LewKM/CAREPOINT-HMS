from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import (
    LAB_PRIORITY_RANK, PRIORITY_RANK, Admission, AdmissionStatus, Appointment, AppointmentStatus, AuditLog, Bed,
    BedStatus, Department, Drug, Encounter, EncounterStatus, Invoice, InvoiceItem, InvoiceStatus, LabOrder, LabStatus,
    Patient, Payment, Prescription, PrescriptionStatus, Role, User, Ward,
)
from ..security import current_user
from ..templating import render
from ..utils import day_bounds, today_local

router = APIRouter()

ACTIVE = (EncounterStatus.WAITING, EncounterStatus.TRIAGED, EncounterStatus.IN_PROGRESS)


def _queue_sorted(encounters):
    return sorted(encounters, key=lambda e: (PRIORITY_RANK[e.priority], e.checked_in_at))


def _todays_appointments(db, start, end, doctor_id=None):
    stmt = (select(Appointment).options(joinedload(Appointment.patient), joinedload(Appointment.doctor))
            .where(Appointment.scheduled_at >= start, Appointment.scheduled_at < end)
            .order_by(Appointment.scheduled_at))
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    return db.scalars(stmt).all()


def _ward_census(db):
    rows = db.execute(
        select(Ward, func.count(Bed.id), func.count(Bed.id).filter(Bed.status == BedStatus.OCCUPIED))
        .join(Bed, Bed.ward_id == Ward.id, isouter=True).group_by(Ward.id).order_by(Ward.name)
    ).all()
    return [{"ward": w, "beds": total, "occupied": occ} for w, total, occ in rows]


def _outstanding(db) -> Decimal:
    billed = db.scalar(select(func.coalesce(func.sum(InvoiceItem.unit_price * InvoiceItem.quantity), 0))
                       .join(Invoice).where(Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID])))
    paid = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0))
                     .join(Invoice).where(Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID])))
    return Decimal(billed) - Decimal(paid)


def admin_ctx(db, start, end, user):
    month_start, _ = day_bounds(today_local().replace(day=1))
    census = _ward_census(db)
    staff = dict(db.execute(select(User.role, func.count()).where(User.is_active).group_by(User.role)).all())
    dept_load = db.execute(
        select(Department.name, func.count(Appointment.id))
        .join(Appointment, (Appointment.department_id == Department.id) & (Appointment.scheduled_at >= start)
              & (Appointment.scheduled_at < end), isouter=True)
        .where(Department.is_clinical).group_by(Department.id).order_by(func.count(Appointment.id).desc(), Department.name)
    ).all()
    revenue_by_cat = db.execute(
        select(InvoiceItem.category, func.sum(InvoiceItem.unit_price * InvoiceItem.quantity))
        .join(Invoice).where(Invoice.status != InvoiceStatus.VOID, InvoiceItem.created_at >= month_start)
        .group_by(InvoiceItem.category).order_by(func.sum(InvoiceItem.unit_price * InvoiceItem.quantity).desc())
    ).all()
    return dict(
        patients_total=db.scalar(select(func.count(Patient.id))),
        patients_today=db.scalar(select(func.count(Patient.id)).where(Patient.created_at >= start, Patient.created_at < end)),
        appointments_today=db.scalar(select(func.count(Appointment.id)).where(Appointment.scheduled_at >= start, Appointment.scheduled_at < end)),
        visits_active=db.scalar(select(func.count(Encounter.id)).where(Encounter.status.in_(ACTIVE))),
        beds_total=sum(c["beds"] for c in census),
        beds_occupied=sum(c["occupied"] for c in census),
        census=census,
        collected_today=db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.received_at >= start, Payment.received_at < end)),
        collected_month=db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.received_at >= month_start)),
        outstanding=_outstanding(db),
        low_stock=db.scalar(select(func.count(Drug.id)).where(Drug.is_active, Drug.stock_quantity <= Drug.reorder_level)),
        staff=staff,
        dept_load=dept_load,
        revenue_by_cat=revenue_by_cat,
        recent_audit=db.scalars(select(AuditLog).options(joinedload(AuditLog.user)).order_by(AuditLog.id.desc()).limit(8)).all(),
    )


def doctor_ctx(db, start, end, user):
    queue = db.scalars(select(Encounter).options(joinedload(Encounter.patient))
                       .where(Encounter.status.in_(ACTIVE),
                              (Encounter.doctor_id == user.id) | Encounter.doctor_id.is_(None))).all()
    since = start - timedelta(days=2)
    return dict(
        queue=_queue_sorted(queue),
        appointments=_todays_appointments(db, start, end, user.id),
        inpatients=db.scalars(select(Admission).options(joinedload(Admission.patient), joinedload(Admission.bed).joinedload(Bed.ward))
                              .where(Admission.status == AdmissionStatus.ADMITTED, Admission.admitting_doctor_id == user.id)
                              .order_by(Admission.admitted_at)).all(),
        results=db.scalars(select(LabOrder).options(joinedload(LabOrder.patient), joinedload(LabOrder.test))
                           .where(LabOrder.ordered_by_id == user.id, LabOrder.status == LabStatus.COMPLETED,
                                  LabOrder.completed_at >= since)
                           .order_by(LabOrder.is_abnormal.desc(), LabOrder.completed_at.desc()).limit(10)).all(),
        seen_today=db.scalar(select(func.count(Encounter.id)).where(Encounter.doctor_id == user.id,
                                                                     Encounter.completed_at >= start, Encounter.completed_at < end)),
    )


def nurse_ctx(db, start, end, user):
    active = db.scalars(select(Encounter).options(joinedload(Encounter.patient), joinedload(Encounter.doctor))
                        .where(Encounter.status.in_(ACTIVE))).all()
    return dict(
        triage=_queue_sorted([e for e in active if e.status == EncounterStatus.WAITING]),
        awaiting_doctor=len([e for e in active if e.status == EncounterStatus.TRIAGED]),
        census=_ward_census(db),
        inpatients=db.scalars(select(Admission).options(joinedload(Admission.patient), joinedload(Admission.bed).joinedload(Bed.ward),
                                                        joinedload(Admission.admitting_doctor))
                              .where(Admission.status == AdmissionStatus.ADMITTED).order_by(Admission.admitted_at)).all(),
        cleaning=db.scalars(select(Bed).options(joinedload(Bed.ward)).where(Bed.status == BedStatus.CLEANING)).all(),
    )


def receptionist_ctx(db, start, end, user):
    appts = _todays_appointments(db, start, end)
    return dict(
        appointments=appts,
        counts={s: len([a for a in appts if a.status == s]) for s in AppointmentStatus},
        in_queue=db.scalar(select(func.count(Encounter.id)).where(Encounter.status.in_(ACTIVE))),
        recent_patients=db.scalars(select(Patient).order_by(Patient.id.desc()).limit(6)).all(),
    )


def pharmacist_ctx(db, start, end, user):
    soon = today_local() + timedelta(days=60)
    return dict(
        pending=db.scalars(select(Prescription).options(joinedload(Prescription.patient), joinedload(Prescription.doctor),
                                                        joinedload(Prescription.items))
                           .where(Prescription.status == PrescriptionStatus.PENDING).order_by(Prescription.created_at)).unique().all(),
        dispensed_today=db.scalar(select(func.count(Prescription.id)).where(Prescription.dispensed_at >= start, Prescription.dispensed_at < end)),
        low_stock=db.scalars(select(Drug).where(Drug.is_active, Drug.stock_quantity <= Drug.reorder_level).order_by(Drug.stock_quantity)).all(),
        expiring=db.scalars(select(Drug).where(Drug.is_active, Drug.expiry_date.is_not(None), Drug.expiry_date <= soon).order_by(Drug.expiry_date)).all(),
    )


def lab_ctx(db, start, end, user):
    open_orders = db.scalars(select(LabOrder).options(joinedload(LabOrder.patient), joinedload(LabOrder.test))
                             .where(LabOrder.status.in_([LabStatus.ORDERED, LabStatus.COLLECTED]))).all()
    return dict(
        worklist=sorted(open_orders, key=lambda o: (LAB_PRIORITY_RANK[o.priority], o.created_at))[:12],
        to_collect=len([o for o in open_orders if o.status == LabStatus.ORDERED]),
        in_process=len([o for o in open_orders if o.status == LabStatus.COLLECTED]),
        done_today=db.scalar(select(func.count(LabOrder.id)).where(LabOrder.completed_at >= start, LabOrder.completed_at < end)),
        abnormal_today=db.scalar(select(func.count(LabOrder.id)).where(LabOrder.completed_at >= start, LabOrder.completed_at < end, LabOrder.is_abnormal)),
    )


def cashier_ctx(db, start, end, user):
    by_method = db.execute(select(Payment.method, func.sum(Payment.amount), func.count())
                           .where(Payment.received_at >= start, Payment.received_at < end).group_by(Payment.method)).all()
    open_invoices = db.scalars(select(Invoice).options(joinedload(Invoice.patient))
                               .where(Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]))
                               .order_by(Invoice.created_at.desc()).limit(12)).all()
    return dict(
        by_method=by_method,
        collected_today=sum((r[1] for r in by_method), Decimal(0)),
        my_today=db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.received_by_id == user.id, Payment.received_at >= start, Payment.received_at < end)),
        open_invoices=[i for i in open_invoices if i.balance > 0],
        outstanding=_outstanding(db),
    )


CONTEXTS = {
    Role.ADMIN: admin_ctx, Role.DOCTOR: doctor_ctx, Role.NURSE: nurse_ctx, Role.RECEPTIONIST: receptionist_ctx,
    Role.PHARMACIST: pharmacist_ctx, Role.LAB_TECH: lab_ctx, Role.CASHIER: cashier_ctx,
}


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    start, end = day_bounds()
    ctx = CONTEXTS[user.role](db, start, end, user)
    return render(request, "dashboard/index.html", partial=f"dashboard/_{user.role.value}.html", **ctx)
