from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models import Admission, AuditLog, Bed, BedStatus, Department, Role, User, Ward, WardType
from ..security import hash_password, require, temporary_password
from ..services.audit import audit
from ..templating import flash, redirect, render
from ..utils import blank, get_or_404, paginate, parse_decimal, parse_int

router = APIRouter(prefix="/admin")

LICENSED_ROLES = {Role.DOCTOR, Role.NURSE, Role.PHARMACIST, Role.LAB_TECH}


# ---------------------------------------------------------------- staff

@router.get("/users")
def users(request: Request, role: str = "", q: str = "", user: User = Depends(require("admin.users")),
          db: Session = Depends(get_db)):
    stmt = select(User).options(joinedload(User.department)).order_by(User.is_active.desc(), User.full_name)
    if role in Role._value2member_map_:
        stmt = stmt.where(User.role == role)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(like), User.email.ilike(like)))
    counts = dict(db.execute(select(User.role, func.count()).where(User.is_active).group_by(User.role)).all())
    return render(request, "admin/users.html", users=db.scalars(stmt).all(), role=role, q=q, counts=counts)


def _departments(db):
    return db.scalars(select(Department).order_by(Department.name)).all()


@router.get("/users/new")
def new_user(request: Request, user: User = Depends(require("admin.users")), db: Session = Depends(get_db)):
    return render(request, "admin/user_form.html", staff=None, values={}, errors={}, departments=_departments(db))


def _clean_user(db, staff: User | None, full_name, email, phone, role, department_id, license_no) -> tuple[dict, dict]:
    errors = {}
    email = email.strip().lower()
    if not full_name.strip():
        errors["full_name"] = "Required."
    if "@" not in email:
        errors["email"] = "Enter a valid email."
    elif db.scalar(select(User.id).where(func.lower(User.email) == email, User.id != (staff.id if staff else 0))):
        errors["email"] = "Another account already uses this email."
    if role not in Role._value2member_map_:
        errors["role"] = "Choose a role."
    dept = db.get(Department, int(department_id)) if department_id.isdigit() else None
    if role == Role.DOCTOR and not dept:
        errors["department_id"] = "Doctors need a department so patients can be booked to them."
    if role in LICENSED_ROLES and not license_no.strip():
        errors["license_no"] = "Clinical staff need a practising licence / registration number."
    data = dict(full_name=full_name.strip(), email=email, phone=blank(phone), role=Role(role) if role in Role._value2member_map_ else None,
                department_id=dept.id if dept else None, license_no=blank(license_no))
    return data, errors


@router.post("/users/new")
def create_user(request: Request, full_name: Annotated[str, Form()] = "", email: Annotated[str, Form()] = "",
                phone: Annotated[str, Form()] = "", role: Annotated[str, Form()] = "",
                department_id: Annotated[str, Form()] = "", license_no: Annotated[str, Form()] = "",
                user: User = Depends(require("admin.users")), db: Session = Depends(get_db)):
    data, errors = _clean_user(db, None, full_name, email, phone, role, department_id, license_no)
    if errors:
        return render(request, "admin/user_form.html", status_code=422, staff=None, errors=errors,
                      departments=_departments(db), values=dict(full_name=full_name, email=email, phone=phone, role=role,
                                                                department_id=department_id, license_no=license_no))
    temp = temporary_password()
    staff = User(**data, password_hash=hash_password(temp), must_change_password=True)
    db.add(staff)
    db.flush()
    audit(db, request, user, "user.created", "user", staff.id, f"{staff.email} as {staff.role.value}")
    db.commit()
    return render(request, "admin/temp_password.html", staff=staff, temp=temp, created=True)


@router.get("/users/{user_id}")
def edit_user(request: Request, user_id: int, user: User = Depends(require("admin.users")), db: Session = Depends(get_db)):
    staff = get_or_404(db, User, user_id)
    values = dict(full_name=staff.full_name, email=staff.email, phone=staff.phone or "", role=staff.role.value,
                  department_id=str(staff.department_id or ""), license_no=staff.license_no or "")
    return render(request, "admin/user_form.html", staff=staff, values=values, errors={}, departments=_departments(db))


@router.post("/users/{user_id}")
def update_user(request: Request, user_id: int, full_name: Annotated[str, Form()] = "", email: Annotated[str, Form()] = "",
                phone: Annotated[str, Form()] = "", role: Annotated[str, Form()] = "",
                department_id: Annotated[str, Form()] = "", license_no: Annotated[str, Form()] = "",
                user: User = Depends(require("admin.users")), db: Session = Depends(get_db)):
    staff = get_or_404(db, User, user_id)
    data, errors = _clean_user(db, staff, full_name, email, phone, role, department_id, license_no)
    if staff.id == user.id and data["role"] != Role.ADMIN:
        errors["role"] = "You can't remove your own administrator role."
    if errors:
        return render(request, "admin/user_form.html", status_code=422, staff=staff, errors=errors,
                      departments=_departments(db), values=dict(full_name=full_name, email=email, phone=phone, role=role,
                                                                department_id=department_id, license_no=license_no))
    if data["role"] != staff.role:
        staff.auth_version += 1  # role change takes effect immediately, not at next login
    for k, v in data.items():
        setattr(staff, k, v)
    audit(db, request, user, "user.updated", "user", staff.id, f"role={staff.role.value}")
    db.commit()
    flash(request, f"{staff.full_name}'s account saved.")
    return redirect("/admin/users")


@router.post("/users/{user_id}/active")
def toggle_active(request: Request, user_id: int, user: User = Depends(require("admin.users")),
                  db: Session = Depends(get_db)):
    staff = get_or_404(db, User, user_id)
    if staff.id == user.id:
        raise HTTPException(409, "You can't deactivate your own account.")
    staff.is_active = not staff.is_active
    staff.auth_version += 1
    audit(db, request, user, "user.activated" if staff.is_active else "user.deactivated", "user", staff.id)
    db.commit()
    flash(request, f"{staff.full_name} {'re-activated' if staff.is_active else 'deactivated and signed out'}.")
    return redirect(f"/admin/users/{staff.id}")


@router.post("/users/{user_id}/reset-password")
def reset_password(request: Request, user_id: int, user: User = Depends(require("admin.users")),
                   db: Session = Depends(get_db)):
    staff = get_or_404(db, User, user_id)
    temp = temporary_password()
    staff.password_hash = hash_password(temp)
    staff.must_change_password = True
    staff.auth_version += 1
    staff.failed_logins, staff.locked_until = 0, None
    audit(db, request, user, "user.password_reset", "user", staff.id)
    db.commit()
    return render(request, "admin/temp_password.html", staff=staff, temp=temp, created=False)


# ---------------------------------------------------------------- departments

@router.get("/departments")
def departments(request: Request, edit: int | None = None, user: User = Depends(require("admin.config")),
                db: Session = Depends(get_db)):
    rows = db.execute(select(Department, func.count(User.id)).join(User, (User.department_id == Department.id) & User.is_active, isouter=True)
                      .group_by(Department.id).order_by(Department.name)).all()
    return render(request, "admin/departments.html", rows=rows, editing=db.get(Department, edit) if edit else None)


@router.post("/departments")
def save_department(request: Request, id: Annotated[str, Form()] = "", name: Annotated[str, Form()] = "",
                    code: Annotated[str, Form()] = "", consultation_fee: Annotated[str, Form()] = "0",
                    description: Annotated[str, Form()] = "", is_clinical: Annotated[str, Form()] = "",
                    user: User = Depends(require("admin.config")), db: Session = Depends(get_db)):
    dept = get_or_404(db, Department, int(id)) if id.isdigit() else Department()
    code = code.strip().upper()
    try:
        fee = parse_decimal(consultation_fee) or 0
    except ValueError:
        fee = None
    clash = db.scalar(select(Department.id).where(or_(func.lower(Department.name) == name.strip().lower(),
                                                      Department.code == code), Department.id != (dept.id or 0)))
    if not name.strip() or not code or fee is None or clash:
        flash(request, "Name and code are required and must be unique; the fee must be a number.", "error")
        return redirect(f"/admin/departments?edit={dept.id}" if dept.id else "/admin/departments")
    dept.name, dept.code, dept.consultation_fee = name.strip(), code, fee
    dept.description, dept.is_clinical = blank(description), is_clinical == "on"
    db.add(dept)
    db.flush()
    audit(db, request, user, "department.saved", "department", dept.id, dept.name)
    db.commit()
    flash(request, f"{dept.name} saved.")
    return redirect("/admin/departments")


# ---------------------------------------------------------------- wards

@router.get("/wards")
def wards(request: Request, user: User = Depends(require("admin.config")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Ward).options(joinedload(Ward.beds)).order_by(Ward.name)).unique().all()
    return render(request, "admin/wards.html", wards=rows, ward_types=list(WardType))


@router.post("/wards")
def save_ward(request: Request, id: Annotated[str, Form()] = "", name: Annotated[str, Form()] = "",
              code: Annotated[str, Form()] = "", ward_type: Annotated[str, Form()] = "",
              daily_rate: Annotated[str, Form()] = "0", user: User = Depends(require("admin.config")),
              db: Session = Depends(get_db)):
    ward = get_or_404(db, Ward, int(id)) if id.isdigit() else Ward()
    code = code.strip().upper()
    try:
        rate = parse_decimal(daily_rate) or 0
    except ValueError:
        rate = None
    clash = db.scalar(select(Ward.id).where(or_(func.lower(Ward.name) == name.strip().lower(), Ward.code == code),
                                            Ward.id != (ward.id or 0)))
    if not name.strip() or not code or ward_type not in WardType._value2member_map_ or rate is None or clash:
        flash(request, "Name, a unique code, type and a numeric daily rate are required.", "error")
        return redirect("/admin/wards")
    ward.name, ward.code, ward.ward_type, ward.daily_rate = name.strip(), code, WardType(ward_type), rate
    db.add(ward)
    db.flush()
    audit(db, request, user, "ward.saved", "ward", ward.id, ward.name)
    db.commit()
    flash(request, f"{ward.name} saved.")
    return redirect("/admin/wards")


@router.post("/wards/{ward_id}/beds")
def add_beds(request: Request, ward_id: int, count: Annotated[str, Form()] = "1",
             user: User = Depends(require("admin.config")), db: Session = Depends(get_db)):
    ward = get_or_404(db, Ward, ward_id)
    try:
        n = parse_int(count, minimum=1, maximum=100) or 1
    except ValueError:
        flash(request, "Add between 1 and 100 beds at a time.", "error")
        return redirect("/admin/wards")
    existing = {b.label for b in ward.beds}
    i, added = 1, []
    while len(added) < n:
        label = f"{ward.code}-{i:02d}"
        if label not in existing:
            db.add(Bed(ward_id=ward.id, label=label, status=BedStatus.AVAILABLE))
            added.append(label)
        i += 1
    audit(db, request, user, "ward.beds_added", "ward", ward.id, f"{n} beds")
    db.commit()
    flash(request, f"Added {n} bed(s) to {ward.name}: {added[0]}{' – ' + added[-1] if n > 1 else ''}.")
    return redirect("/admin/wards")


@router.post("/wards/beds/{bed_id}/delete")
def delete_bed(request: Request, bed_id: int, user: User = Depends(require("admin.config")), db: Session = Depends(get_db)):
    bed = get_or_404(db, Bed, bed_id)
    if bed.status == BedStatus.OCCUPIED or db.scalar(select(Admission.id).where(Admission.bed_id == bed.id).limit(1)):
        flash(request, "This bed has admission history, so it can't be removed — set it to maintenance instead.", "error")
        return redirect("/admin/wards")
    audit(db, request, user, "ward.bed_removed", "ward", bed.ward_id, bed.label)
    db.delete(bed)
    db.commit()
    flash(request, f"Bed {bed.label} removed.")
    return redirect("/admin/wards")


# ---------------------------------------------------------------- audit

@router.get("/audit")
def audit_log(request: Request, q: str = "", user_id: str = "", page: int = 1,
              user: User = Depends(require("admin.audit")), db: Session = Depends(get_db)):
    stmt = select(AuditLog).options(joinedload(AuditLog.user)).order_by(AuditLog.id.desc())
    if q.strip():
        stmt = stmt.where(or_(AuditLog.action.ilike(f"%{q.strip()}%"), AuditLog.detail.ilike(f"%{q.strip()}%")))
    if user_id.isdigit():
        stmt = stmt.where(AuditLog.user_id == int(user_id))
    staff = db.scalars(select(User).order_by(User.full_name)).all()
    return render(request, "admin/audit.html", page=paginate(db, stmt, page), q=q, user_id=user_id, staff=staff)
