import json
from pathlib import Path
import numpy as np, pandas as pd, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt
ROOT = Path(__file__).resolve().parents[2]; OUT = Path(__file__).resolve().parent
S = json.load(open(OUT / 'summary.json'))['evaluated_on_half_B']
names = [('no_scaling', 'No scaling\n(T = 1)', '#94a3b8'), ('dev_fitted', 'Calibration-set T\n(T = 0.743)', '#ea7c1a'),
         ('half_A_fitted', 'Fitted on other half\n(median T = 3.03)', '#16a34a')]
fig, ax = plt.subplots(1, 3, figsize=(13, 4.2))
for a, (m, lab) in zip(ax, [('log_loss', 'Log-loss'), ('ece', 'ECE'), ('brier', 'Brier score')]):
    for i, (n, nl, c) in enumerate(names):
        s = S[f'{n}_{m}']; a.bar(i, s['median'], color=c, width=.65)
        a.errorbar(i, s['median'], yerr=[[s['median'] - s['p2_5']], [s['p97_5'] - s['median']]], color='#1e293b', capsize=5)
        a.text(i, s['p97_5'] * 1.02, f"{s['median']:.3f}", ha='center', va='bottom', fontsize=10)
    a.set_xticks(range(3)); a.set_xticklabels([x[1] for x in names], fontsize=9); a.set_title(f'{lab} (lower is better)', fontsize=11)
    a.spines[['top', 'right']].set_visible(False); a.set_ylim(0, max(S[f'{n}_{m}']['p97_5'] for n, _, _ in names) * 1.18)
fig.suptitle('Temperature scaling evaluated on held-out halves of the test folder (1,000 patient-level splits; bars = median, lines = 95% range)', fontsize=11)
fig.tight_layout(); fig.savefig(OUT / 'calibration_split_test.png', dpi=200)

d = pd.read_csv(ROOT / 'results/v2/gate_decisions_test.csv')
g = json.load(open(ROOT / 'results/v2/gate_params.json'))
ind = d.distance <= g['distance_threshold']; pn = d.probability >= 0.5
nb = ind & (d.confidence >= g['confidence_threshold'])
rt = ind & ((pn & (d.probability >= g['confidence_threshold'])) | (~pn & (d.probability <= 0.0198)))
def cm(m): s = d[m]; p = s.probability >= 0.5; return np.array([[((s.label == 0) & ~p).sum(), ((s.label == 0) & p).sum()], [((s.label == 1) & ~p).sum(), ((s.label == 1) & p).sum()]])
mats = [('Ungated (n = 618)', cm(d.label >= 0)), (f'Notebook gate: accepted (n = {nb.sum()})', cm(nb)), (f'Re-tuned gate, post-hoc: accepted (n = {rt.sum()})', cm(rt))]
fig, ax = plt.subplots(1, 3, figsize=(14, 4.4))
for a, (t, M) in zip(ax, mats):
    a.imshow(M, cmap='Blues', vmin=0, vmax=M.max() * 1.1)
    for i in range(2):
        for j in range(2): a.text(j, i, M[i, j], ha='center', va='center', fontsize=17, color='white' if M[i, j] > M.max() * .55 else '#0f172a')
    a.set_xticks([0, 1]); a.set_xticklabels(['predicted normal', 'predicted pneumonia']); a.set_yticks([0, 1]); a.set_yticklabels(['actual normal', 'actual pneumonia'])
    sens = M[1, 1] / M[1].sum(); spec = M[0, 0] / M[0].sum(); acc = np.trace(M) / M.sum()
    a.set_title(t, fontsize=11); a.set_xlabel(f'sensitivity {sens:.3f} · specificity {spec:.3f} · accuracy {acc:.3f}', fontsize=9.5)
fig.suptitle('DenseNet121 confusion matrices on the test set', fontsize=12)
fig.tight_layout(); fig.savefig(OUT / 'confusion_matrices.png', dpi=200)
for t, M in mats: print(t, M.tolist())
