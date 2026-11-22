# Gate re-tuning (separate from the evaluated bundle)

This branch holds a post-hoc change to the DenseNet121 confidence gate. It is kept apart
from `main`, which runs the gate exactly as the v2 notebook evaluated it.

## What changed

Only one number: the normal-side cut-off. Weights, temperature, distance threshold and the
pneumonia cut-off are identical to the notebook (`results/v2/gate_params.json`).

| | Notebook gate (`main`) | Re-tuned gate (this branch) |
|---|---|---|
| CLEARED when P(pneumonia) <= | 0.0002 (1 - 0.99978) | **0.0198** |
| FLAGGED_URGENT when P(pneumonia) >= | 0.99978 | 0.99978 |
| REJECTED when Mahalanobis distance > | 841.66 | 841.66 |
| Model version string | `densenet121@63cd29f2fc9e+gate.c763cb3b` | `densenet121@63cd29f2fc9e+gate.8be7a9b4` |

## How the cut-off was chosen

`scripts/retune_gate.py` scores the 1,346 development images the notebook held out from
training (`sel` + `cal` splits of `results/v2/manifest.csv`, patient-disjoint from test)
and takes the highest cut-off, to 4 decimals, that clears no in-distribution pneumonia
there. No retraining. The re-tuning was done **after** the notebook gate's test results
were known, so its test results are a secondary, post-hoc analysis, not a pre-specified
evaluation.

## Test-set results (618 images, 231 normal / 387 pneumonia)

| | Notebook gate | Re-tuned gate |
|---|---|---|
| CLEARED | 36 | 80 |
| FLAGGED_URGENT | 339 | 339 |
| ESCALATED | 201 | 157 |
| REJECTED | 42 | 42 |
| Pneumonia among CLEARED | 0 / 36 | 0 / 80 |
| Specificity on accepted | 0.720 (36/50) | 0.851 (80/94) |
| Accuracy on accepted | 0.963 (361/375) | 0.967 (405/419) |

## Agreement with the evaluated bundle

`app_vs_notebook.csv` scores every test image with `backend/inference.py` and compares it
with the notebook's `results/v2/gate_decisions_test.csv`:

- Notebook gate in the app: same triage state as the notebook on **618 / 618** images
  (max probability difference 5.3e-6, max distance difference 0.005).
- Re-tuned gate: identical on 574 images; the other **44** are all normal images that move
  from ESCALATED to CLEARED (app P(pneumonia) between 0.00024 and 0.0162). No other state
  changes.

## Limitations

- Post-hoc and fitted on 1,346 development images from the same single hospital; it does
  not establish clinical safety and is not a substitute for local recalibration.
- "0 pneumonia among 80 CLEARED" has a 95% upper bound of about 4.5% (exact, images
  treated as independent); several test children contribute more than one image, so a
  patient-level bound is wider.
