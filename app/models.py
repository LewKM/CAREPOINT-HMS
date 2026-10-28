import enum
import math
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(e: type[enum.Enum]) -> SAEnum:
    # Stored as varchar so adding a value never needs a Postgres type migration.
    return SAEnum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


Money = Numeric(12, 2)
Timestamp = DateTime(timezone=True)


class LabeledEnum(enum.StrEnum):
    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


# ---------------------------------------------------------------- enums

class Role(LabeledEnum):
    ADMIN = "admin"
    DOCTOR = "doctor"
    NURSE = "nurse"
    RECEPTIONIST = "receptionist"
    PHARMACIST = "pharmacist"
    LAB_TECH = "lab_technician"
    CASHIER = "cashier"

    @property
    def label(self) -> str:
        return "Hospital Administrator" if self is Role.ADMIN else super().label


class Gender(LabeledEnum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class AppointmentStatus(LabeledEnum):
    SCHEDULED = "scheduled"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class VisitType(LabeledEnum):
    OUTPATIENT = "outpatient"
    EMERGENCY = "emergency"
    FOLLOW_UP = "follow_up"


class Priority(LabeledEnum):
    ROUTINE = "routine"
    URGENT = "urgent"
    EMERGENCY = "emergency"


PRIORITY_RANK = {Priority.EMERGENCY: 0, Priority.URGENT: 1, Priority.ROUTINE: 2}


class EncounterStatus(LabeledEnum):
    WAITING = "waiting"          # checked in, awaiting triage
    TRIAGED = "triaged"          # vitals taken, awaiting doctor
    IN_PROGRESS = "in_progress"  # with the doctor
    COMPLETED = "completed"


class WardType(LabeledEnum):
    GENERAL = "general"
    PEDIATRIC = "pediatric"
    MATERNITY = "maternity"
    SURGICAL = "surgical"
    ICU = "icu"
    ISOLATION = "isolation"

    @property
    def label(self) -> str:
        return "ICU" if self is WardType.ICU else super().label


class BedStatus(LabeledEnum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    CLEANING = "cleaning"
    MAINTENANCE = "maintenance"


class AdmissionStatus(LabeledEnum):
    ADMITTED = "admitted"
    DISCHARGED = "discharged"


class NoteType(LabeledEnum):
    NURSING = "nursing"
    PROGRESS = "progress"


class PrescriptionStatus(LabeledEnum):
    PENDING = "pending"
    DISPENSED = "dispensed"
    CANCELLED = "cancelled"


class LabPriority(LabeledEnum):
    ROUTINE = "routine"
    URGENT = "urgent"
    STAT = "stat"

    @property
    def label(self) -> str:
        return "STAT" if self is LabPriority.STAT else super().label


LAB_PRIORITY_RANK = {LabPriority.STAT: 0, LabPriority.URGENT: 1, LabPriority.ROUTINE: 2}


class LabStatus(LabeledEnum):
    ORDERED = "ordered"
    COLLECTED = "collected"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class InvoiceStatus(LabeledEnum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


class ChargeCategory(LabeledEnum):
    CONSULTATION = "consultation"
    LABORATORY = "laboratory"
    PHARMACY = "pharmacy"
    WARD = "ward"
    PROCEDURE = "procedure"
    OTHER = "other"


class PaymentMethod(LabeledEnum):
    CASH = "cash"
    MPESA = "mpesa"
    CARD = "card"
    INSURANCE = "insurance"
    BANK = "bank"

    @property
    def label(self) -> str:
        return "M-Pesa" if self is PaymentMethod.MPESA else super().label


# ---------------------------------------------------------------- organisation

class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    consultation_fee: Mapped[Decimal] = mapped_column(Money, default=0)
    is_clinical: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    staff: Mapped[list["User"]] = relationship(back_populates="department")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum(Role), index=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    license_no: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on password change/reset so every other session for this user is dropped.
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(Timestamp)
    last_login_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    department: Mapped[Department | None] = relationship(back_populates="staff")

    @property
    def display_name(self) -> str:
        return f"Dr. {self.full_name}" if self.role == Role.DOCTOR else self.full_name

    @property
    def initials(self) -> str:
        parts = [p for p in self.full_name.split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"


# ---------------------------------------------------------------- patients

class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    mrn: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), index=True)
    gender: Mapped[Gender] = mapped_column(_enum(Gender))
    date_of_birth: Mapped[date] = mapped_column(Date)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    national_id: Mapped[str | None] = mapped_column(String(32), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    blood_group: Mapped[str | None] = mapped_column(String(4))
    allergies: Mapped[str | None] = mapped_column(Text)
    chronic_conditions: Mapped[str | None] = mapped_column(Text)
    next_of_kin_name: Mapped[str | None] = mapped_column(String(160))
    next_of_kin_phone: Mapped[str | None] = mapped_column(String(32))
    next_of_kin_relation: Mapped[str | None] = mapped_column(String(40))
    insurance_provider: Mapped[str | None] = mapped_column(String(120))
    insurance_number: Mapped[str | None] = mapped_column(String(64))
    registered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def age(self) -> int:
        today = date.today()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    scheduled_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=20)
    reason: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[AppointmentStatus] = mapped_column(_enum(AppointmentStatus), default=AppointmentStatus.SCHEDULED, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    patient: Mapped[Patient] = relationship()
    doctor: Mapped[User] = relationship(foreign_keys=[doctor_id])
    department: Mapped[Department] = relationship()
    encounter: Mapped["Encounter | None"] = relationship(back_populates="appointment")


# ---------------------------------------------------------------- clinical

class Encounter(Base):
    """A single hospital visit: check-in → triage → consultation → completion."""

    __tablename__ = "encounters"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"), unique=True)
    visit_type: Mapped[VisitType] = mapped_column(_enum(VisitType), default=VisitType.OUTPATIENT)
    priority: Mapped[Priority] = mapped_column(_enum(Priority), default=Priority.ROUTINE)
    status: Mapped[EncounterStatus] = mapped_column(_enum(EncounterStatus), default=EncounterStatus.WAITING, index=True)
    chief_complaint: Mapped[str | None] = mapped_column(Text)
    history: Mapped[str | None] = mapped_column(Text)
    examination: Mapped[str | None] = mapped_column(Text)
    diagnosis: Mapped[str | None] = mapped_column(Text)
    treatment_plan: Mapped[str | None] = mapped_column(Text)
    follow_up_date: Mapped[date | None] = mapped_column(Date)
    checked_in_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    patient: Mapped[Patient] = relationship()
    doctor: Mapped[User | None] = relationship(foreign_keys=[doctor_id])
    department: Mapped[Department] = relationship()
    appointment: Mapped[Appointment | None] = relationship(back_populates="encounter")
    vitals: Mapped[list["Vitals"]] = relationship(back_populates="encounter", order_by="Vitals.recorded_at.desc()")
    prescriptions: Mapped[list["Prescription"]] = relationship(back_populates="encounter", order_by="Prescription.created_at.desc()")
    lab_orders: Mapped[list["LabOrder"]] = relationship(back_populates="encounter", order_by="LabOrder.created_at.desc()")
    admission: Mapped["Admission | None"] = relationship(back_populates="encounter")

    @property
    def is_active(self) -> bool:
        return self.status != EncounterStatus.COMPLETED


class Vitals(Base):
    __tablename__ = "vitals"

    id: Mapped[int] = mapped_column(primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    recorded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    temperature: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    systolic: Mapped[int | None] = mapped_column(Integer)
    diastolic: Mapped[int | None] = mapped_column(Integer)
    pulse: Mapped[int | None] = mapped_column(Integer)
    respiratory_rate: Mapped[int | None] = mapped_column(Integer)
    spo2: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    notes: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    encounter: Mapped[Encounter] = relationship(back_populates="vitals")
    recorded_by: Mapped[User | None] = relationship()

    @property
    def bmi(self) -> float | None:
        if self.weight_kg and self.height_cm:
            m = float(self.height_cm) / 100
            return round(float(self.weight_kg) / (m * m), 1)
        return None

    @property
    def flags(self) -> list[str]:
        """Out-of-range readings for an adult, shown as warnings to clinicians."""
        out = []
        if self.temperature is not None and not (36.0 <= float(self.temperature) <= 37.5):
            out.append("temperature")
        if self.systolic is not None and not (90 <= self.systolic <= 139):
            out.append("bp")
        if self.diastolic is not None and not (60 <= self.diastolic <= 89) and "bp" not in out:
            out.append("bp")
        if self.pulse is not None and not (60 <= self.pulse <= 100):
            out.append("pulse")
        if self.respiratory_rate is not None and not (12 <= self.respiratory_rate <= 20):
            out.append("respiratory_rate")
        if self.spo2 is not None and self.spo2 < 95:
            out.append("spo2")
        return out


# ---------------------------------------------------------------- wards

class Ward(Base):
    __tablename__ = "wards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)
    ward_type: Mapped[WardType] = mapped_column(_enum(WardType))
    daily_rate: Mapped[Decimal] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    beds: Mapped[list["Bed"]] = relationship(back_populates="ward", order_by="Bed.label")


class Bed(Base):
    __tablename__ = "beds"
    __table_args__ = (UniqueConstraint("ward_id", "label", name="uq_beds_ward_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"), index=True)
    label: Mapped[str] = mapped_column(String(16))
    status: Mapped[BedStatus] = mapped_column(_enum(BedStatus), default=BedStatus.AVAILABLE)

    ward: Mapped[Ward] = relationship(back_populates="beds")


class Admission(Base):
    __tablename__ = "admissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), unique=True)
    bed_id: Mapped[int] = mapped_column(ForeignKey("beds.id"))
    admitting_doctor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[AdmissionStatus] = mapped_column(_enum(AdmissionStatus), default=AdmissionStatus.ADMITTED, index=True)
    admitted_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    # Start of the not-yet-billed stretch; moves forward when a ward transfer bills the old ward.
    billed_from: Mapped[datetime | None] = mapped_column(Timestamp)
    discharged_at: Mapped[datetime | None] = mapped_column(Timestamp)
    discharged_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    discharge_condition: Mapped[str | None] = mapped_column(String(40))
    discharge_summary: Mapped[str | None] = mapped_column(Text)

    patient: Mapped[Patient] = relationship()
    encounter: Mapped[Encounter] = relationship(back_populates="admission")
    bed: Mapped[Bed] = relationship()
    admitting_doctor: Mapped[User] = relationship(foreign_keys=[admitting_doctor_id])
    discharged_by: Mapped[User | None] = relationship(foreign_keys=[discharged_by_id])
    notes: Mapped[list["ClinicalNote"]] = relationship(back_populates="admission", order_by="ClinicalNote.created_at.desc()")

    @staticmethod
    def _day_count(start: datetime, end: datetime) -> int:
        # Any part of a day counts, minimum one.
        return max(1, math.ceil((end - start).total_seconds() / 86400))

    @property
    def days(self) -> int:
        """Length of stay."""
        return self._day_count(self.admitted_at, self.discharged_at or utcnow())

    @property
    def billable_days(self) -> int:
        return self._day_count(self.billed_from or self.admitted_at, self.discharged_at or utcnow())


class ClinicalNote(Base):
    __tablename__ = "clinical_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    note_type: Mapped[NoteType] = mapped_column(_enum(NoteType))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    admission: Mapped[Admission] = relationship(back_populates="notes")
    author: Mapped[User] = relationship()


# ---------------------------------------------------------------- pharmacy

class Drug(Base):
    __tablename__ = "drugs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    generic_name: Mapped[str | None] = mapped_column(String(160))
    form: Mapped[str] = mapped_column(String(40))  # tablet, capsule, syrup, injection…
    strength: Mapped[str | None] = mapped_column(String(40))
    unit_price: Mapped[Decimal] = mapped_column(Money, default=0)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer, default=20)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())

    @property
    def label(self) -> str:
        return " ".join(p for p in (self.name, self.strength, f"({self.form})") if p)

    @property
    def is_low(self) -> bool:
        return self.stock_quantity <= self.reorder_level

    @property
    def expires_soon(self) -> bool:
        return bool(self.expiry_date and (self.expiry_date - date.today()).days <= 60)


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drugs.id"), index=True)
    change: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(40))  # restock, dispense, adjustment
    reference: Mapped[str | None] = mapped_column(String(120))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    user: Mapped[User | None] = relationship()


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[PrescriptionStatus] = mapped_column(_enum(PrescriptionStatus), default=PrescriptionStatus.PENDING, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    dispensed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    dispensed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    cancel_reason: Mapped[str | None] = mapped_column(String(255))

    encounter: Mapped[Encounter] = relationship(back_populates="prescriptions")
    patient: Mapped[Patient] = relationship()
    doctor: Mapped[User] = relationship(foreign_keys=[doctor_id])
    dispensed_by: Mapped[User | None] = relationship(foreign_keys=[dispensed_by_id])
    items: Mapped[list["PrescriptionItem"]] = relationship(back_populates="prescription", cascade="all, delete-orphan")


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"), index=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drugs.id"))
    dose: Mapped[str] = mapped_column(String(60))
    frequency: Mapped[str] = mapped_column(String(60))
    duration_days: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    instructions: Mapped[str | None] = mapped_column(String(255))

    prescription: Mapped[Prescription] = relationship(back_populates="items")
    drug: Mapped[Drug] = relationship()


# ---------------------------------------------------------------- laboratory

class LabTest(Base):
    __tablename__ = "lab_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(60))  # Haematology, Biochemistry…
    specimen: Mapped[str] = mapped_column(String(60))
    unit: Mapped[str | None] = mapped_column(String(30))
    reference_range: Mapped[str | None] = mapped_column(String(120))
    price: Mapped[Decimal] = mapped_column(Money, default=0)
    turnaround_hours: Mapped[int] = mapped_column(Integer, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class LabOrder(Base):
    __tablename__ = "lab_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("lab_tests.id"))
    ordered_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    priority: Mapped[LabPriority] = mapped_column(_enum(LabPriority), default=LabPriority.ROUTINE)
    clinical_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[LabStatus] = mapped_column(_enum(LabStatus), default=LabStatus.ORDERED, index=True)
    result_value: Mapped[str | None] = mapped_column(Text)
    result_notes: Mapped[str | None] = mapped_column(Text)
    is_abnormal: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_at: Mapped[datetime | None] = mapped_column(Timestamp)
    collected_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    performed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    encounter: Mapped[Encounter] = relationship(back_populates="lab_orders")
    patient: Mapped[Patient] = relationship()
    test: Mapped[LabTest] = relationship()
    ordered_by: Mapped[User] = relationship(foreign_keys=[ordered_by_id])
    performed_by: Mapped[User | None] = relationship(foreign_keys=[performed_by_id])


# ---------------------------------------------------------------- billing

class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str | None] = mapped_column(String(24), unique=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[int | None] = mapped_column(ForeignKey("encounters.id"), index=True)
    status: Mapped[InvoiceStatus] = mapped_column(_enum(InvoiceStatus), default=InvoiceStatus.OPEN, index=True)
    void_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    patient: Mapped[Patient] = relationship()
    encounter: Mapped[Encounter | None] = relationship()
    items: Mapped[list["InvoiceItem"]] = relationship(back_populates="invoice", order_by="InvoiceItem.id", lazy="selectin")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice", order_by="Payment.received_at", lazy="selectin")

    @property
    def total(self) -> Decimal:
        return sum((i.amount for i in self.items), Decimal("0"))

    @property
    def paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), Decimal("0"))

    @property
    def balance(self) -> Decimal:
        return self.total - self.paid


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    category: Mapped[ChargeCategory] = mapped_column(_enum(ChargeCategory))
    description: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    invoice: Mapped[Invoice] = relationship(back_populates="items")

    @property
    def amount(self) -> Decimal:
        return self.unit_price * self.quantity


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    method: Mapped[PaymentMethod] = mapped_column(_enum(PaymentMethod))
    reference: Mapped[str | None] = mapped_column(String(80))
    received_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    received_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, index=True)

    invoice: Mapped[Invoice] = relationship(back_populates="payments")
    received_by: Mapped[User | None] = relationship()


# ---------------------------------------------------------------- audit

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, index=True)

    user: Mapped[User | None] = relationship()
