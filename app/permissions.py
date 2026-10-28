"""Single source of truth for who may do what.

Routes guard with `require("perm")`, templates hide controls with `can(user, "perm")`,
and the sidebar is filtered by the same keys — change access here and nowhere else.
"""
from .models import Role

ADMIN, DOCTOR, NURSE, RECEPTION, PHARMACIST, LAB, CASHIER = (
    Role.ADMIN, Role.DOCTOR, Role.NURSE, Role.RECEPTIONIST, Role.PHARMACIST, Role.LAB_TECH, Role.CASHIER,
)
EVERYONE = frozenset(Role)

PERMISSIONS: dict[str, frozenset[Role]] = {
    "dashboard": EVERYONE,
    # Patient registry — demographics only. Clinical detail is gated separately.
    "patients.view": EVERYONE,
    "patients.register": frozenset({ADMIN, RECEPTION}),
    "patients.edit": frozenset({ADMIN, RECEPTION}),
    "patients.clinical": frozenset({DOCTOR, NURSE}),
    # Scheduling & front desk
    "appointments.view": frozenset({ADMIN, DOCTOR, NURSE, RECEPTION}),
    "appointments.manage": frozenset({ADMIN, RECEPTION}),
    "visits.view": frozenset({ADMIN, DOCTOR, NURSE, RECEPTION}),
    "visits.checkin": frozenset({ADMIN, RECEPTION}),
    # Clinical care
    "triage.record": frozenset({NURSE, DOCTOR}),
    "consult.perform": frozenset({DOCTOR}),
    "prescriptions.write": frozenset({DOCTOR}),
    "lab.order": frozenset({DOCTOR}),
    # Wards
    "wards.view": frozenset({ADMIN, DOCTOR, NURSE, RECEPTION}),
    "wards.admit": frozenset({DOCTOR}),
    "wards.discharge": frozenset({DOCTOR}),
    "wards.notes": frozenset({DOCTOR, NURSE}),
    "wards.transfer": frozenset({DOCTOR, NURSE}),
    "wards.beds": frozenset({ADMIN, NURSE}),
    # Pharmacy
    "pharmacy.view": frozenset({ADMIN, PHARMACIST}),
    "pharmacy.dispense": frozenset({PHARMACIST}),
    "pharmacy.inventory": frozenset({ADMIN, PHARMACIST}),
    # Laboratory
    "lab.view": frozenset({LAB}),
    "lab.process": frozenset({LAB}),
    "lab.catalog": frozenset({ADMIN, LAB}),
    # Billing
    "billing.view": frozenset({ADMIN, CASHIER}),
    "billing.collect": frozenset({CASHIER}),
    "billing.charge": frozenset({ADMIN, CASHIER}),
    "billing.void": frozenset({ADMIN}),
    # Administration
    "admin.users": frozenset({ADMIN}),
    "admin.config": frozenset({ADMIN}),
    "admin.audit": frozenset({ADMIN}),
}

ROLE_SUMMARIES: dict[Role, str] = {
    ADMIN: "Staff accounts, departments, wards, price lists, billing oversight and the audit trail. No access to clinical notes.",
    DOCTOR: "Consultations, diagnoses, prescriptions, lab orders, admissions and discharges.",
    NURSE: "Triage and vitals, ward rounds, nursing notes, bed transfers and bed turnover.",
    RECEPTION: "Patient registration, appointments, check-in and walk-in visits.",
    PHARMACIST: "Dispensing prescriptions and managing drug stock.",
    LAB: "Sample collection, result entry and the lab test catalogue.",
    CASHIER: "Patient bills, payments, receipts and daily collections.",
}


def can(user, perm: str) -> bool:
    if user is None:
        return False
    if perm not in PERMISSIONS:
        raise KeyError(f"Unknown permission {perm!r}")
    return user.role in PERMISSIONS[perm]
