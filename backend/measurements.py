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
