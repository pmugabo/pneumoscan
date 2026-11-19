"""Plausibility checks for a child's measurements, entered in order: age, sex, length/height,
weight, then head circumference (under 3 years) and mid-upper-arm circumference (6-59 months).

Each value is checked against limits that depend on the values entered before it:
  - "typical" ranges approximate the WHO/CDC 3rd-97th centiles; a value outside them is
    possible (prematurity, malnutrition, obesity, dwarfism) but must be confirmed with a reason;
  - "allowed" ranges are wider physiological limits; a value outside them is rejected.
The same tables are used in the browser (frontend/assets/measurements.js).
"""
from __future__ import annotations

from typing import Optional

MAX_AGE_MONTHS = 216
AGES = (0, 3, 6, 12, 24, 36, 60, 96, 120, 156, 180, 216)
# age months -> (typical low, typical high, allowed low, allowed high)
WEIGHT_KG = ((2.4, 4.4, 0.4, 6.5), (4.4, 7.8, 1.5, 11), (5.8, 9.8, 2.5, 14), (7.3, 12.0, 3.5, 18),
             (9.4, 15.3, 5, 24), (11.0, 18.3, 6, 30), (14.0, 24.0, 8, 45), (20, 35, 11, 70),
             (24, 45, 13, 90), (32, 68, 17, 120), (40, 78, 22, 140), (46, 90, 25, 160))
HEIGHT_CM = ((45.5, 54.5, 25, 60), (56, 66, 40, 72), (62, 72, 48, 80), (69, 80, 55, 90),
             (81, 93, 65, 105), (88, 102, 72, 115), (100, 118, 82, 135), (116, 138, 95, 160),
             (126, 150, 103, 172), (140, 170, 115, 195), (148, 182, 120, 205), (152, 190, 125, 215))
HEAD_AGES = (0, 6, 12, 24, 36)
HEAD_CM = ((32, 37, 22, 42), (40.5, 46, 33, 52), (43, 48.5, 36, 55), (45.5, 50.5, 38, 57),
           (46.5, 51.5, 39, 58))
MUAC_CM = (12.5, 18.0, 7.0, 25.0)
HEAD_MAX_MONTHS = 36
MUAC_MONTHS = (6, 59)
BMI_ALLOWED = (8.0, 50.0)


def _interp(ages, table, m: float) -> tuple:
    m = max(ages[0], min(ages[-1], m))
    for i in range(len(ages) - 1):
        a, b = ages[i], ages[i + 1]
        if a <= m <= b:
            f = (m - a) / (b - a)
            return tuple(round(x + (y - x) * f, 1) for x, y in zip(table[i], table[i + 1]))
    return table[-1]


def bmi_typical(m: float) -> tuple:
    return (11.0, 21.0) if m < 24 else (12.0, 24.0) if m < 120 else (14.0, 32.0)


def ranges(age_months: float, height_cm: Optional[float] = None) -> dict:
    """Limits for each measurement given the values before it."""
    out = {"weight_kg": _interp(AGES, WEIGHT_KG, age_months),
           "height_cm": _interp(AGES, HEIGHT_CM, age_months)}
    if age_months < HEAD_MAX_MONTHS:
        out["head_circumference_cm"] = _interp(HEAD_AGES, HEAD_CM, age_months)
    if MUAC_MONTHS[0] <= age_months <= MUAC_MONTHS[1]:
        out["muac_cm"] = MUAC_CM
    if height_cm:
        h = (height_cm / 100) ** 2
        tl, th = bmi_typical(age_months)
        w = out["weight_kg"]
        # weight must also fit the body size: typical and allowed BMI for this height
        out["weight_kg"] = (round(max(w[0], tl * h), 1), round(min(w[1], th * h), 1),
                            round(max(w[2], BMI_ALLOWED[0] * h), 1), round(min(w[3], BMI_ALLOWED[1] * h), 1))
        if "head_circumference_cm" in out:
            hc = out["head_circumference_cm"]
            out["head_circumference_cm"] = hc[:3] + (round(min(hc[3], 0.85 * height_cm), 1),)
    for k, (tl, th, al, ah) in out.items():
        al, ah = min(al, ah), max(al, ah)
        tl, th = max(al, min(tl, th)), min(ah, max(tl, th))
        out[k] = (tl, th, al, ah)
    return out


LABELS = {"weight_kg": ("Weight", "kg"), "height_cm": ("Length/height", "cm"),
          "head_circumference_cm": ("Head circumference", "cm"), "muac_cm": ("MUAC", "cm")}


def check(age_months: Optional[int], values: dict) -> tuple[list, list]:
    """Return (errors, warnings). Errors block saving; warnings need a confirmed reason."""
    errors, warnings = [], []
    present = {k: v for k, v in values.items() if v is not None}
    if age_months is None:
        if present:
            errors.append("Enter the child's age before any measurements.")
        return errors, warnings
    if not 0 <= age_months <= MAX_AGE_MONTHS:
        errors.append("Age must be between 0 months and 18 years.")
        return errors, warnings
    if "weight_kg" in present and "height_cm" not in present:
        errors.append("Enter length/height before weight.")
    r = ranges(age_months, present.get("height_cm"))
    for key in ("height_cm", "weight_kg", "head_circumference_cm", "muac_cm"):
        if key not in present:
            continue
        name, unit = LABELS[key]
        v = present[key]
        if key not in r:
            errors.append(f"{name} is not recorded at this age.")
            continue
        tl, th, al, ah = r[key]
        if not al <= v <= ah:
            errors.append(f"{name} {v:g} {unit} is not possible for this child "
                          f"(allowed {al:g}-{ah:g} {unit}).")
        elif not tl <= v <= th:
            warnings.append(f"{name} {v:g} {unit} is outside the usual range "
                            f"{tl:g}-{th:g} {unit} for this child.")
    return errors, warnings


# ------------------------------------------------------------------ vital signs at the scan
# Respiratory rate bands by age (months): (typical low, typical high, allowed low, allowed high).
# "Typical" already includes the fast breathing expected with pneumonia (WHO limits 60/50/40);
# values beyond it are possible (severe distress, exhaustion) but must be confirmed.
RR_BANDS = ((2, (30, 80, 10, 120)), (12, (24, 70, 10, 100)), (60, (20, 60, 8, 90)),
            (144, (14, 45, 6, 70)), (10_000, (12, 35, 5, 60)))
RR_ANY_AGE = (12, 80, 5, 120)
SPO2_PCT = (85, 100, 50, 100)        # below 85 % happens (severe pneumonia, cyanotic heart disease)
TEMP_C = (35.0, 40.5, 30.0, 43.0)    # below 35 °C: hypothermia (neonates); above 40.5 °C: hyperpyrexia
VITAL_LABELS = {"respiratory_rate": ("Respiratory rate", "/min"), "spo2": ("SpO2", "%"),
                "temperature_c": ("Temperature", "°C")}


def age_now_months(patient: dict, at: Optional[str] = None) -> Optional[float]:
    """Age today from the age recorded when the patient was registered or last measured."""
    from datetime import datetime, timezone
    months = patient.get("age_months")
    if months is None and patient.get("age") is not None:
        months = patient["age"] * 12
    if months is None:
        return None
    since = patient.get("measured_at") or patient.get("created_at")
    try:
        then = datetime.fromisoformat(since)
        now = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
        months += max(0.0, (now - then).days / 30.44)
    except (TypeError, ValueError):
        pass
    return float(months)


def vital_ranges(age_months: Optional[float]) -> dict:
    rr = RR_ANY_AGE if age_months is None else next(r for lim, r in RR_BANDS if age_months < lim)
    return {"respiratory_rate": rr, "spo2": SPO2_PCT, "temperature_c": TEMP_C}


def check_vitals(age_months: Optional[float], values: dict, signs=()) -> tuple[list, list]:
    """Return (errors, warnings) for the vital signs recorded with a scan. Errors block the
    scan; warnings (unusual values, or values that do not fit together) need a reason."""
    errors, warnings = [], []
    rr, spo2, temp = values.get("respiratory_rate"), values.get("spo2"), values.get("temperature_c")
    if temp is not None and 86 <= temp <= 113:
        errors.append(f"Temperature {temp:g} looks like °F ({(temp - 32) / 1.8:.1f} °C). Enter it in °C.")
        temp = None
    if spo2 is not None and 0 < spo2 <= 1:
        errors.append("Enter SpO2 as a percentage, for example 94, not 0.94.")
        spo2 = None
    r = vital_ranges(age_months)
    for key, v in (("respiratory_rate", rr), ("spo2", spo2), ("temperature_c", temp)):
        if v is None:
            continue
        name, unit = VITAL_LABELS[key]
        tl, th, al, ah = r[key]
        if not al <= v <= ah:
            errors.append(f"{name} {v:g} {unit} is not possible (allowed {al:g}-{ah:g} {unit}).")
        elif not tl <= v <= th:
            warnings.append(f"{name} {v:g} {unit} is outside the usual range {tl:g}-{th:g} {unit}"
                            + (" for this age." if key == "respiratory_rate" and age_months is not None else "."))
    if errors:
        return errors, warnings
    if "cyanosis" in (signs or ()) and spo2 is not None and spo2 >= 94:
        warnings.append(f"Central cyanosis is recorded but SpO2 is {spo2:g} %. Check the probe and the child.")
    if spo2 is not None and spo2 < 85 and rr is not None and rr < r["respiratory_rate"][0]:
        warnings.append(f"Very low SpO2 ({spo2:g} %) with a slow breathing rate ({rr:g}/min) is unusual. "
                        "Recount the breaths and check the probe; a tiring child needs urgent care.")
    return errors, warnings
