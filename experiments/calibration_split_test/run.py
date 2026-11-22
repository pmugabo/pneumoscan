"""Post-hoc supplementary experiment: does temperature scaling help when it is fitted on data
from the same distribution as the evaluation data?

The original test folder is split by child into two halves; T is fitted on one half and evaluated
on the other. Repeated over many random splits. Uses the saved test outputs only; the model,
the gate and the main results are not changed.
"""
import json
from pathlib import Path
import numpy as np, pandas as pd
from scipy.optimize import minimize_scalar

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
T_DEV = json.load(open(ROOT / 'results/v2/gate_params.json'))['temperature']
REPEATS, SEED, BINS = 1000, 0, 15

d = pd.read_csv(ROOT / 'results/v2/gate_decisions_test.csv')
p = d.probability.clip(1e-12, 1 - 1e-12).to_numpy()
z = T_DEV * np.log(p / (1 - p))          # raw logit: saved probability was sigma(z / T_DEV)
y = d.label.to_numpy().astype(float)
pid = d.patient_id.to_numpy()

def prob(z, T): return 1 / (1 + np.exp(-z / T))
def nll(z, y, T):
    q = np.clip(prob(z, T), 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))
def brier(z, y, T): return float(np.mean((prob(z, T) - y) ** 2))
def ece(z, y, T, bins=BINS):
    q = prob(z, T); conf = np.maximum(q, 1 - q); correct = ((q >= 0.5) == (y == 1))
    edges = np.linspace(0.5, 1, bins + 1); e = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi) if lo > 0.5 else (conf >= lo) & (conf <= hi)
        if m.any(): e += m.mean() * abs(correct[m].mean() - conf[m].mean())
    return float(e)
def fit_T(z, y): return float(minimize_scalar(lambda t: nll(z, y, t), bounds=(0.05, 20), method='bounded').x)

full = {name: {'T': T, 'log_loss': nll(z, y, T), 'brier': brier(z, y, T), 'ece': ece(z, y, T)}
        for name, T in [('no_scaling', 1.0), ('dev_fitted', T_DEV)]}

patients = pd.Series(y, index=pid).groupby(level=0).max()   # child label: pneumonia if any image is
rng = np.random.default_rng(SEED); rows = []
for r in range(REPEATS):
    a = set()
    for lab in (0, 1):
        ids = patients[patients == lab].index.to_numpy(); rng.shuffle(ids); a |= set(ids[: len(ids) // 2])
    ma = np.array([i in a for i in pid]); mb = ~ma
    Ta = fit_T(z[ma], y[ma]); row = {'repeat': r, 'T_fitted_on_half_A': Ta, 'n_A': int(ma.sum()), 'n_B': int(mb.sum())}
    for name, T in [('no_scaling', 1.0), ('dev_fitted', T_DEV), ('half_A_fitted', Ta)]:
        row[f'{name}_log_loss'] = nll(z[mb], y[mb], T); row[f'{name}_brier'] = brier(z[mb], y[mb], T)
        row[f'{name}_ece'] = ece(z[mb], y[mb], T)
    rows.append(row)
R = pd.DataFrame(rows); R.to_csv(OUT / 'splits.csv', index=False)

def summ(c): return {'median': float(R[c].median()), 'p2_5': float(R[c].quantile(.025)), 'p97_5': float(R[c].quantile(.975))}
summary = {
    'note': 'Post-hoc supplementary analysis on the original test folder; not part of the frozen evaluation.',
    'repeats': REPEATS, 'seed': SEED, 'ece_bins': BINS, 'dev_temperature': T_DEV,
    'full_test_set': full,
    'T_fitted_on_half_A': summ('T_fitted_on_half_A'),
    'evaluated_on_half_B': {f'{n}_{m}': summ(f'{n}_{m}') for n in ('no_scaling', 'dev_fitted', 'half_A_fitted')
                            for m in ('log_loss', 'brier', 'ece')},
    'share_of_splits_half_A_T_beats_no_scaling': {m: float((R[f'half_A_fitted_{m}'] < R[f'no_scaling_{m}']).mean())
                                                  for m in ('log_loss', 'brier', 'ece')},
}
json.dump(summary, open(OUT / 'summary.json', 'w'), indent=2)
print(json.dumps(summary, indent=2))
