/* Plausibility limits for a child's measurements; mirrors backend/measurements.py.
   Each limit depends on the values entered before it: age -> length/height -> weight (also
   checked against body size) -> head circumference (< 3 y) and MUAC (6-59 months). */
const GROWTH = {
  ages: [0, 3, 6, 12, 24, 36, 60, 96, 120, 156, 180, 216],
  weight: [[2.4, 4.4, 0.4, 6.5], [4.4, 7.8, 1.5, 11], [5.8, 9.8, 2.5, 14], [7.3, 12.0, 3.5, 18],
           [9.4, 15.3, 5, 24], [11.0, 18.3, 6, 30], [14.0, 24.0, 8, 45], [20, 35, 11, 70],
           [24, 45, 13, 90], [32, 68, 17, 120], [40, 78, 22, 140], [46, 90, 25, 160]],
  height: [[45.5, 54.5, 25, 60], [56, 66, 40, 72], [62, 72, 48, 80], [69, 80, 55, 90],
           [81, 93, 65, 105], [88, 102, 72, 115], [100, 118, 82, 135], [116, 138, 95, 160],
           [126, 150, 103, 172], [140, 170, 115, 195], [148, 182, 120, 205], [152, 190, 125, 215]],
  headAges: [0, 6, 12, 24, 36],
  head: [[32, 37, 22, 42], [40.5, 46, 33, 52], [43, 48.5, 36, 55], [45.5, 50.5, 38, 57], [46.5, 51.5, 39, 58]],
  muac: [12.5, 18.0, 7.0, 25.0],
};
const r1 = x => Math.round(x * 10) / 10;
function growthInterp(ages, table, m) {
  m = Math.max(ages[0], Math.min(ages[ages.length - 1], m));
  for (let i = 0; i < ages.length - 1; i++) {
    const a = ages[i], b = ages[i + 1];
    if (m >= a && m <= b) { const f = (m - a) / (b - a); return table[i].map((x, j) => r1(x + (table[i + 1][j] - x) * f)); }
  }
  return table[table.length - 1];
}
const bmiTypical = m => m < 24 ? [11, 21] : m < 120 ? [12, 24] : [14, 32];
function growthRanges(months, heightCm) {
  const out = {weight_kg: growthInterp(GROWTH.ages, GROWTH.weight, months), height_cm: growthInterp(GROWTH.ages, GROWTH.height, months)};
  if (months < 36) out.head_circumference_cm = growthInterp(GROWTH.headAges, GROWTH.head, months);
  if (months >= 6 && months <= 59) out.muac_cm = GROWTH.muac.slice();
  if (heightCm) {
    const h = (heightCm / 100) ** 2, [tl, th] = bmiTypical(months), w = out.weight_kg;
    out.weight_kg = [r1(Math.max(w[0], tl * h)), r1(Math.min(w[1], th * h)), r1(Math.max(w[2], 8 * h)), r1(Math.min(w[3], 50 * h))];
    if (out.head_circumference_cm) out.head_circumference_cm[3] = r1(Math.min(out.head_circumference_cm[3], 0.85 * heightCm));
  }
  for (const k in out) {
    let [tl, th, al, ah] = out[k];
    [al, ah] = [Math.min(al, ah), Math.max(al, ah)];
    out[k] = [Math.max(al, Math.min(tl, th)), Math.min(ah, Math.max(tl, th)), al, ah];
  }
  return out;
}
