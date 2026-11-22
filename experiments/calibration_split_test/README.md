# Supplementary calibration experiment (post-hoc)

**Question:** temperature scaling fitted on the calibration set (T = 0.743) made test-set calibration slightly worse. Is that because the calibration set and the test folder differ, or because temperature scaling does not suit this model at all?

**Method:** the 618 test images (403 children) are split 50/50 **by child** (stratified by label). T is fitted by minimising log-loss on one half and evaluated on the other half. This is repeated over 1,000 random splits (seed 0). Each half is compared with no scaling (T = 1) and with the development T (0.743). The script uses the saved test outputs in `results/v2/gate_decisions_test.csv`; the model, gate and main results are unchanged.

```
python experiments/calibration_split_test/run.py      # summary.json, splits.csv
python experiments/calibration_split_test/figures.py  # calibration_split_test.png, confusion_matrices.png
```

## Results (held-out half; median and 95% range over 1,000 splits)

| | No scaling (T = 1) | Calibration-set T = 0.743 | T fitted on the other half (median T = 3.03) |
|---|---|---|---|
| Log-loss | 0.545 (0.431–0.659) | 0.709 (0.556–0.863) | **0.311** (0.277–0.357) |
| ECE (15 bins) | 0.110 (0.088–0.132) | 0.117 (0.095–0.140) | **0.056** (0.036–0.077) |
| Brier | 0.114 (0.093–0.134) | 0.118 (0.096–0.139) | **0.096** (0.083–0.112) |

A T fitted on test-like data beat no scaling in 100% of splits for log-loss and Brier, and 99.9% for ECE.

**Interpretation:** temperature scaling works when it is fitted on data from the same distribution it is used on. On test-like data the fitted T is about 3 (above 1), so the model needs *softening*. On the calibration set, where the model is almost always right, the fit chose T < 1 instead. The earlier failure is therefore caused by the shift between the development data and the test folder, not by the method itself. This supports recalibrating on local data before any use at a new site, although recalibration alone cannot guarantee accuracy.

**Limits:** post-hoc, chosen after the main results were seen; both halves come from the same test folder, so this is not an independent evaluation; images from the same child are kept together, but the test set is still from one hospital. Triage decisions do not depend on T, because τc was fitted on the scaled outputs, so none of the triage counts change.

## Confusion matrices (test set)

| | TN | FP | FN | TP |
|---|---|---|---|---|
| Ungated (618) | 151 | 80 | 1 | 386 |
| Notebook gate, accepted (375) | 36 | 14 | 0 | 325 |
| Re-tuned gate (post-hoc), accepted (419) | 80 | 14 | 0 | 325 |

ECE uses 15 equal-width confidence bins, which gives 0.103 / 0.112 on the full test set (the notebook reports 0.101 / 0.110 with its own binning).
