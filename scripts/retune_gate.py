"""Refit the confidence gate as two one-sided thresholds on the validation split.

The original notebook picked one probability threshold for 90% validation sensitivity
(0.9755) and then reused it as a symmetric confidence gate, so a normal-looking image had
to reach P(pneumonia) <= 0.0245 before it was cleared. This script reproduces the
notebook's patient-level validation split, scores it with the deployed model and fits:

    normal_threshold    highest P(pneumonia) at which auto-clearing misses <= 1% of
                        validation pneumonia cases
    pneumonia_threshold kept at the original 0.9755, so urgent flags are as precise as before

Thresholds are fitted on validation only; the test set is scored once to report results.

Needs pandas (pip install pandas). Usage (dataset: Kermany et al. 2018, Mendeley rscbjbr9sj v2, or the Kaggle copy):
    python -m scripts.retune_gate --data /path/to/chest_xray
"""
import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from PIL import Image
from torchvision import transforms

from backend.inference import engine

ROOT = Path(__file__).resolve().parent.parent
SEED = 42
MAX_MISSED = 0.01


def patient_id(p):
    n = Path(p).name
    m = re.search(r"(person\d+)", n) or re.search(r"(IM-\d+)", n)
    return m.group(1) if m else Path(p).stem


def splits(data: Path):
    rows = []
    for p in sorted(data.rglob("*")):
        if p.suffix.lower() not in {".jpeg", ".jpg", ".png"} or "__MACOSX" in p.parts:
            continue
        parts = {q.upper() for q in p.parts}
        label = 0 if "NORMAL" in parts else 1 if "PNEUMONIA" in parts else None
        folder = "test" if "TEST" in parts else "train"   # Kaggle's 16-image val folder joins train
        if label is not None:
            rows.append((str(p), label, patient_id(p), folder))
    df = pd.DataFrame(rows, columns=["path", "label", "patient_id", "folder"])
    seen = defaultdict(list)
    for p in df.path:
        seen[hashlib.md5(Path(p).read_bytes()).hexdigest()].append(p)
    dupes = {p for ps in seen.values() for p in ps[1:]}
    df = df[~df.path.isin(dupes)]
    pool, test = df[df.folder == "train"], df[df.folder == "test"]
    pat_label = pool.groupby("patient_id")["label"].agg(lambda s: int(s.mean() >= 0.5))
    shuffled = pd.Series(pat_label.index).sample(frac=1, random_state=SEED).tolist()
    val_ids = []
    for c in (0, 1):
        ids = [p for p in shuffled if pat_label[p] == c]
        val_ids += ids[int(0.8 * len(ids)):]
    return pool[pool.patient_id.isin(set(val_ids))], test


def score(frame):
    tf = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor(),
                             transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])
    m, T = engine.model, engine.gate["temperature"]
    probs, dists, paths = [], [], frame.path.tolist()
    for i in range(0, len(paths), 32):
        x = torch.stack([tf(Image.open(p).convert("RGB")) for p in paths[i:i + 32]])
        with torch.no_grad():
            feats = torch.flatten(m.avgpool(m.features(x)), 1)
            probs += torch.sigmoid(m.classifier(feats).squeeze(1) / T).tolist()
        dists += [engine.mahalanobis(f.numpy()) for f in feats]
    return np.array(probs), np.array(dists), frame.label.to_numpy()


def evaluate(p, d, y, t_norm, t_pneu, d_thr):
    ind = d <= d_thr
    clr, urg = ind & (p <= t_norm), ind & (p >= t_pneu)
    acc = clr | urg
    tp, fn = int((urg & (y == 1)).sum()), int((clr & (y == 1)).sum())
    fp, tn = int((urg & (y == 0)).sum()), int((clr & (y == 0)).sum())
    pa, ya = p[acc], y[acc]
    npos, nneg = int(ya.sum()), int(len(ya) - ya.sum())
    ranks = np.empty(len(pa)); ranks[np.argsort(pa)] = np.arange(1, len(pa) + 1)
    auc = (ranks[ya == 1].sum() - npos * (npos + 1) / 2) / (npos * nneg) if npos and nneg else None
    prec, sens = tp / max(1, tp + fp), tp / max(1, tp + fn)
    return {"n": int(len(y)), "accepted": int(acc.sum()),
            "escalated_low_confidence": int((ind & ~acc).sum()),
            "rejected_out_of_distribution": int((~ind).sum()),
            "autonomy_rate": float(acc.mean()),
            "sensitivity": sens, "specificity": tn / max(1, tn + fp),
            "precision": prec, "npv": tn / max(1, tn + fn),
            "accuracy": (tp + tn) / max(1, len(ya)), "f1": 2 * prec * sens / max(1e-9, prec + sens),
            "auc": None if auc is None else float(auc),
            "tp": tp, "tn": tn, "fp": fp, "fn": fn}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    args = ap.parse_args()
    val, test = splits(args.data)
    print(f"validation {len(val)} images, test {len(test)} images")
    vp, vd, vy = score(val)
    tp_, td, ty = score(test)
    gate = engine.gate
    d_thr = gate["distance_threshold"]
    t_pneu = max(gate["confidence_threshold"], 1 - gate["confidence_threshold"])
    ind = vd <= d_thr
    p, y = vp[ind], vy[ind]
    budget = MAX_MISSED * (y == 1).sum()
    t_norm = max(float(t) for t in np.unique(np.round(p, 4))
                 if t < 0.5 and ((p <= t) & (y == 1)).sum() <= budget)
    gate.update({"pneumonia_threshold": t_pneu, "normal_threshold": t_norm,
                 "gate_rule": f"pneumonia if p >= {t_pneu:.4f}; normal if p <= {t_norm:.4f}; "
                              "else escalate. normal_threshold: highest value missing <= 1% "
                              "of validation pneumonia; fitted on validation only."})
    (ROOT / "models" / "gate_params.json").write_text(json.dumps(gate, indent=1))
    res_path = ROOT / "results" / "gating_results.json"
    res = json.loads(res_path.read_text())
    res.setdefault("previous_symmetric_gate", {k: res[k] for k in
                   ("accepted", "escalated_low_confidence", "rejected_out_of_distribution",
                    "autonomy_rate", "gated_accepted_only") if k in res})
    new_test = evaluate(tp_, td, ty, t_norm, t_pneu, d_thr)
    res.update({k: new_test[k] for k in ("accepted", "escalated_low_confidence",
                                         "rejected_out_of_distribution", "autonomy_rate")})
    res["gated_accepted_only"] = {**res.get("gated_accepted_only", {}),
                                  **{k: new_test[k] for k in ("sensitivity", "specificity", "precision",
                                     "npv", "accuracy", "f1", "auc", "tp", "tn", "fp", "fn")},
                                  "threshold": None,
                                  "n": new_test["accepted"]}
    res["validation_two_sided_gate"] = evaluate(vp, vd, vy, t_norm, t_pneu, d_thr)
    res_path.write_text(json.dumps(res, indent=2))
    print(json.dumps({"normal_threshold": t_norm, "pneumonia_threshold": t_pneu,
                      "test": new_test}, indent=1))


if __name__ == "__main__":
    main()
