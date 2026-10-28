"""Seed reference data (departments, wards, lab tests, formulary) and the first administrator.

    python -m scripts.seed                 # reference data + admin with a random temporary password
    python -m scripts.seed --demo          # also one account per role, sample patients and today's bookings

Safe to re-run: existing rows (matched by code / email) are left alone.
"""
import argparse
import random
from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    Appointment, Bed, BedStatus, Department, Drug, Gender, LabTest, Patient, Role, StockMovement, User, Ward, WardType,
)
from app.security import hash_password, temporary_password
from app.utils import local_to_utc, today_local

DEMO_PASSWORD = "CarePoint#2026"

DEPARTMENTS = [
    ("General Outpatient (OPD)", "OPD", 500, True, "Walk-in and booked general consultations"),
    ("Accident & Emergency", "AE", 1000, True, "24-hour emergency care"),
    ("Internal Medicine", "MED", 1500, True, "Adult medical specialist clinic"),
    ("Paediatrics", "PAED", 1000, True, "Children under 13"),
    ("Obstetrics & Gynaecology", "OBGYN", 1500, True, "Antenatal, maternity and gynaecology"),
    ("Surgery", "SURG", 2000, True, "General surgical clinic"),
    ("Laboratory", "LAB", 0, False, "Diagnostic laboratory"),
    ("Pharmacy", "PHARM", 0, False, "Dispensary and stores"),
    ("Finance", "FIN", 0, False, "Billing and cashiers"),
    ("Administration", "ADMIN", 0, False, "Hospital management"),
]

WARDS = [
    ("Male Medical Ward", "MMW", WardType.GENERAL, 2500, 10),
    ("Female Medical Ward", "FMW", WardType.GENERAL, 2500, 10),
    ("Paediatric Ward", "PW", WardType.PEDIATRIC, 2000, 8),
    ("Maternity Ward", "MAT", WardType.MATERNITY, 3000, 8),
    ("Surgical Ward", "SW", WardType.SURGICAL, 3500, 6),
    ("Intensive Care Unit", "ICU", WardType.ICU, 15000, 4),
]

LAB_TESTS = [
    ("FBC", "Full Blood Count", "Haematology", "Whole blood (EDTA)", None, "Hb 12–17 g/dL; WBC 4–11 ×10⁹/L", 800, 2),
    ("ESR", "Erythrocyte Sedimentation Rate", "Haematology", "Whole blood (EDTA)", "mm/hr", "0–20", 300, 2),
    ("BGRP", "Blood Group & Rhesus", "Haematology", "Whole blood (EDTA)", None, None, 400, 1),
    ("MPS", "Malaria Parasites (Blood Slide)", "Parasitology", "Capillary blood", None, "No parasites seen", 300, 1),
    ("MRDT", "Malaria Rapid Diagnostic Test", "Parasitology", "Capillary blood", None, "Negative", 250, 1),
    ("STOOL", "Stool Microscopy (O/C)", "Parasitology", "Stool", None, "No ova or cysts seen", 300, 2),
    ("RBS", "Random Blood Sugar", "Biochemistry", "Capillary blood", "mmol/L", "3.9–7.8", 200, 1),
    ("FBS", "Fasting Blood Sugar", "Biochemistry", "Capillary blood", "mmol/L", "3.9–5.6", 200, 1),
    ("HBA1C", "HbA1c", "Biochemistry", "Whole blood (EDTA)", "%", "< 5.7", 1800, 24),
    ("UEC", "Urea, Electrolytes & Creatinine", "Biochemistry", "Serum", None, "Creat 60–110 µmol/L", 1500, 4),
    ("LFT", "Liver Function Tests", "Biochemistry", "Serum", None, "ALT < 40 U/L", 1800, 4),
    ("LIPID", "Lipid Profile", "Biochemistry", "Serum", "mmol/L", "TC < 5.2", 1500, 4),
    ("URINE", "Urinalysis", "Urinalysis", "Urine", None, "Nil abnormal", 300, 1),
    ("UPT", "Pregnancy Test (urine hCG)", "Urinalysis", "Urine", None, "Negative", 200, 1),
    ("WIDAL", "Widal Test", "Serology", "Serum", None, "< 1:80", 500, 2),
    ("HPYL", "H. pylori Stool Antigen", "Serology", "Stool", None, "Negative", 900, 2),
    ("HIV", "HIV 1 & 2 Screening", "Serology", "Capillary blood", None, "Non-reactive", 0, 1),
    ("VDRL", "VDRL (Syphilis)", "Serology", "Serum", None, "Non-reactive", 400, 2),
]

# name, generic, form, strength, price, stock, reorder, expiry offset (days)
DRUGS = [
    ("Panadol", "Paracetamol", "Tablet", "500 mg", 5, 2000, 300, 540),
    ("Amoxil", "Amoxicillin", "Capsule", "500 mg", 12, 800, 200, 400),
    ("Brufen", "Ibuprofen", "Tablet", "400 mg", 8, 600, 150, 365),
    ("Coartem", "Artemether/Lumefantrine", "Tablet", "20/120 mg", 30, 480, 120, 300),
    ("Glucophage", "Metformin", "Tablet", "500 mg", 6, 900, 200, 450),
    ("Norvasc", "Amlodipine", "Tablet", "5 mg", 10, 120, 150, 500),
    ("Losec", "Omeprazole", "Capsule", "20 mg", 15, 400, 100, 380),
    ("Cipro", "Ciprofloxacin", "Tablet", "500 mg", 20, 300, 100, 320),
    ("Flagyl", "Metronidazole", "Tablet", "400 mg", 6, 700, 150, 45),
    ("Rocephin", "Ceftriaxone", "Injection", "1 g", 250, 90, 40, 600),
    ("Normal Saline", "Sodium chloride 0.9%", "Infusion", "500 ml", 150, 140, 50, 700),
    ("Ventolin", "Salbutamol", "Inhaler", "100 mcg", 450, 25, 20, 420),
    ("Zyrtec", "Cetirizine", "Tablet", "10 mg", 10, 350, 100, 400),
    ("ORS", "Oral rehydration salts", "Sachet", "20.5 g", 25, 500, 100, 600),
    ("Zinc", "Zinc sulphate", "Tablet", "20 mg", 5, 400, 100, 30),
    ("Diclofenac", "Diclofenac sodium", "Tablet", "50 mg", 6, 18, 100, 400),
]

STAFF = [
    ("Grace Wanjiru", "admin@carepoint.local", Role.ADMIN, "ADMIN", None),
    ("Daniel Otieno", "d.otieno@carepoint.local", Role.DOCTOR, "OPD", "KMPDC-A1023"),
    ("Amina Hassan", "a.hassan@carepoint.local", Role.DOCTOR, "MED", "KMPDC-A2210"),
    ("Peter Kiprono", "p.kiprono@carepoint.local", Role.DOCTOR, "PAED", "KMPDC-A3317"),
    ("Mercy Achieng", "m.achieng@carepoint.local", Role.NURSE, "OPD", "NCK-55102"),
    ("Joseph Mutua", "j.mutua@carepoint.local", Role.NURSE, "MED", "NCK-55877"),
    ("Faith Njeri", "reception@carepoint.local", Role.RECEPTIONIST, "OPD", None),
    ("Brian Ouma", "pharmacy@carepoint.local", Role.PHARMACIST, "PHARM", "PPB-7781"),
    ("Lucy Chebet", "lab@carepoint.local", Role.LAB_TECH, "LAB", "KMLTTB-4410"),
    ("Kevin Mwangi", "cashier@carepoint.local", Role.CASHIER, "FIN", None),
]

PATIENTS = [
    ("John", "Kamau", Gender.MALE, date(1978, 4, 12), "0712345678", "22334455", "O+", "Penicillin", "Hypertension", "SHA"),
    ("Mary", "Atieno", Gender.FEMALE, date(1992, 9, 3), "0723456789", "29887766", "A+", None, None, "AAR Insurance"),
    ("Samuel", "Kiptoo", Gender.MALE, date(2019, 1, 25), "0734567890", None, "B+", None, None, None),
    ("Esther", "Wambui", Gender.FEMALE, date(1960, 11, 30), "0745678901", "10293847", "O-", "Sulfa drugs", "Type 2 diabetes", "SHA"),
    ("Ali", "Mohamed", Gender.MALE, date(1985, 6, 18), "0756789012", "24681357", "AB+", None, None, "Jubilee Health"),
    ("Naomi", "Chepkoech", Gender.FEMALE, date(2001, 2, 14), "0767890123", "35792468", None, None, "Asthma", None),
    ("Paul", "Njoroge", Gender.MALE, date(1955, 8, 8), "0778901234", "08172635", "A-", None, "Hypertension, gout", "SHA"),
    ("Lilian", "Moraa", Gender.FEMALE, date(1997, 12, 1), "0789012345", "31415926", "B-", "Latex", None, "Britam"),
]


def seed(demo: bool) -> None:
    db = SessionLocal()
    try:
        depts = {d.code: d for d in db.scalars(select(Department))}
        for name, code, fee, clinical, desc in DEPARTMENTS:
            if code not in depts:
                depts[code] = Department(name=name, code=code, consultation_fee=Decimal(fee), is_clinical=clinical, description=desc)
                db.add(depts[code])
        db.flush()

        existing_wards = set(db.scalars(select(Ward.code)))
        for name, code, wtype, rate, beds in WARDS:
            if code in existing_wards:
                continue
            ward = Ward(name=name, code=code, ward_type=wtype, daily_rate=Decimal(rate))
            ward.beds = [Bed(label=f"{code}-{i:02d}", status=BedStatus.AVAILABLE) for i in range(1, beds + 1)]
            db.add(ward)

        existing_tests = set(db.scalars(select(LabTest.code)))
        for code, name, cat, specimen, unit, ref, price, tat in LAB_TESTS:
            if code not in existing_tests:
                db.add(LabTest(code=code, name=name, category=cat, specimen=specimen, unit=unit, reference_range=ref,
                               price=Decimal(price), turnaround_hours=tat))

        existing_drugs = set(db.scalars(select(Drug.name)))
        new_drugs = []
        for name, generic, form, strength, price, stock, reorder, expiry in DRUGS:
            if name not in existing_drugs:
                d = Drug(name=name, generic_name=generic, form=form, strength=strength, unit_price=Decimal(price),
                         stock_quantity=stock, reorder_level=reorder, expiry_date=date.today() + timedelta(days=expiry))
                db.add(d)
                new_drugs.append(d)
        db.flush()
        for d in new_drugs:
            db.add(StockMovement(drug_id=d.id, change=d.stock_quantity, balance_after=d.stock_quantity,
                                 reason="restock", reference="Opening stock"))

        credentials = []
        staff_rows = STAFF if demo else STAFF[:1]
        users = {u.email: u for u in db.scalars(select(User))}
        for full_name, email, role, dept_code, licence in staff_rows:
            if email in users:
                continue
            password = DEMO_PASSWORD if demo else temporary_password()
            users[email] = User(full_name=full_name, email=email, role=role, department_id=depts[dept_code].id,
                                license_no=licence, password_hash=hash_password(password), must_change_password=not demo)
            db.add(users[email])
            credentials.append((role.label, email, password))
        db.flush()

        if demo and not db.scalar(select(Patient.id).limit(1)):
            reception = users["reception@carepoint.local"]
            patients = []
            for first, last, gender, dob, phone, nid, bg, allergies, chronic, insurer in PATIENTS:
                p = Patient(first_name=first, last_name=last, gender=gender, date_of_birth=dob, phone=phone, national_id=nid,
                            blood_group=bg, allergies=allergies, chronic_conditions=chronic, insurance_provider=insurer,
                            insurance_number=f"M{random.randint(100000, 999999)}" if insurer else None,
                            address="Nairobi", next_of_kin_name="Next of kin", next_of_kin_phone="0700000000",
                            next_of_kin_relation="Spouse", registered_by_id=reception.id)
                db.add(p)
                db.flush()
                p.mrn = f"CP{p.id:06d}"
                patients.append(p)
            doctors = [u for u in users.values() if u.role == Role.DOCTOR]
            day = today_local()
            slots = [time(9, 0), time(9, 30), time(10, 0), time(10, 30), time(11, 0), time(14, 0), time(14, 30), time(15, 0)]
            reasons = ["BP review", "Fever and headache", "Cough for 1 week", "Diabetes follow-up", "Abdominal pain",
                       "Antenatal visit", "Medication refill", "Joint pains"]
            for i, p in enumerate(patients):
                doc = doctors[i % len(doctors)]
                db.add(Appointment(patient_id=p.id, doctor_id=doc.id, department_id=doc.department_id,
                                   scheduled_at=local_to_utc(day, slots[i]), reason=reasons[i], created_by_id=reception.id))

        db.commit()
        print("Seed complete.")
        if credentials:
            print("\nAccounts created:")
            for role, email, password in credentials:
                print(f"  {role:<24} {email:<32} {password}")
            if not demo:
                print("\nThe administrator must change this temporary password at first sign-in.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="add one account per role plus sample patients (dev only)")
    seed(parser.parse_args().demo)
