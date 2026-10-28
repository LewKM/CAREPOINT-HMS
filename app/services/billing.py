"""One bill per visit: every chargeable clinical event lands on the encounter's invoice."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ChargeCategory, Invoice, InvoiceItem, InvoiceStatus, utcnow


def invoice_for(db: Session, patient_id: int, encounter_id: int | None) -> Invoice:
    stmt = select(Invoice).where(Invoice.patient_id == patient_id, Invoice.status != InvoiceStatus.VOID)
    if encounter_id is None:
        stmt = stmt.where(Invoice.encounter_id.is_(None), Invoice.status != InvoiceStatus.PAID)
    else:
        stmt = stmt.where(Invoice.encounter_id == encounter_id)
    inv = db.scalars(stmt.order_by(Invoice.id.desc()).limit(1)).first()
    if inv is None:
        inv = Invoice(patient_id=patient_id, encounter_id=encounter_id, status=InvoiceStatus.OPEN, created_at=utcnow())
        db.add(inv)
        db.flush()
        inv.number = f"INV-{inv.created_at:%Y}-{inv.id:06d}"
    return inv


def charge(db: Session, *, patient_id: int, encounter_id: int | None, category: ChargeCategory,
           description: str, unit_price: Decimal, quantity: int = 1, user_id: int | None = None) -> InvoiceItem:
    inv = invoice_for(db, patient_id, encounter_id)
    item = InvoiceItem(category=category, description=description, quantity=quantity,
                       unit_price=unit_price, created_by_id=user_id)
    inv.items.append(item)
    refresh_status(inv)
    return item


def refresh_status(inv: Invoice) -> None:
    if inv.status == InvoiceStatus.VOID:
        return
    if inv.paid <= 0:
        inv.status = InvoiceStatus.OPEN
    elif inv.paid >= inv.total:
        inv.status = InvoiceStatus.PAID
    else:
        inv.status = InvoiceStatus.PARTIALLY_PAID
