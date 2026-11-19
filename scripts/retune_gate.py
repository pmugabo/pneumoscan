"""Refit the gate's normal-side threshold for the deployed model, without retraining.

The v2 notebook fitted one symmetric confidence threshold (about 0.9998), so a case was
cleared only when P(pneumonia) <= 0.0002 and most normal children were escalated. This
script keeps the model, temperature, distance threshold and pneumonia threshold unchanged
and fits one number on the development images the notebook held out from training
(the `sel` and `cal` splits of results/v2/manifest.csv, patient-disjoint from test):

    normal_threshold   highest P(pneumonia), to 4 decimals, at which auto-clearing misses
                       no in-distribution pneumonia case in those splits

The test split is scored once, only to report results.

Usage (dataset: Kermany et al. 2018, Kaggle "chest_xray" folder):
    python -m scripts.retune_gate --data /path/to/chest_xray
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from PIL import Image

from backend.inference import engine

ROOT = Path(__file__).resolve().parent.parent
FIT_SPLITS = ("sel", "cal")


def load(data: Path, rel: str) -> Image.Image:
    p = data / rel
    if not p.exists():                       # Kaggle copies differ on where val/ lives
        p = data / rel.replace("val/", "train/", 1)
    return Image.open(p)


def score(data: Path, frame: pd.DataFrame):
    T = engine.gate["temperature"]
    probs, dists, files = [], [], frame.file.tolist()
    for i in range(0, len(files), 32):
        x = torch.tensor(np.concatenate([engine.preprocess(load(data, f))[1]
                                         for f in files[i:i + 32]]))
        with torch.no_grad():
            _, logit, emb = engine._forward(x)
        probs += torch.sigmoid(logit.squeeze(1) / T).tolist()
        dists += [engine.mahalanobis(e) for e in emb.numpy()]
    return np.array(probs), np.array(dists), (frame.label == "PNEUMONIA").to_numpy().astype(int)


def evaluate(p, d, y, t_norm, t_pneu, d_thr):
    ind = d <= d_thr
    clr, urg = ind & (p <= t_norm), ind & (p >= t_pneu)
    acc = clr | urg
    tp, fn = int((urg & (y == 1)).sum()), int((clr & (y == 1)).sum())
    fp, tn = int((urg & (y == 0)).sum()), int((clr & (y == 0)).sum())
    prec, sens = tp / max(1, tp + fp), tp / max(1, tp + fn)
    return {"n": int(len(y)), "accepted": int(acc.sum()),
            "escalated_low_confidence": int((ind & ~acc).sum()),
            "rejected_out_of_distribution": int((~ind).sum()),
            "autonomy_rate": float(acc.mean()),
            "final_states": {"CLEARED": int(clr.sum()), "FLAGGED_URGENT": int(urg.sum()),
                             "ESCALATED": int((ind & ~acc).sum()), "REJECTED": int((~ind).sum())},
            "sensitivity": sens, "specificity": tn / max(1, tn + fp),
            "precision": prec, "npv": tn / max(1, tn + fn),
            "accuracy": (tp + tn) / max(1, int(acc.sum())),
            "f1": 2 * prec * sens / max(1e-9, prec + sens),
            "tp": tp, "tn": tn, "fp": fp, "fn": fn}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    args = ap.parse_args()
    if engine.model is None:
        raise SystemExit("No model loaded; put the weights and gate files in models/.")
    manifest = pd.read_csv(ROOT / "results" / "v2" / "manifest.csv")
    dev = manifest[manifest.split.isin(FIT_SPLITS)]
    test = manifest[manifest.split == "test"]
    print(f"fitting on {len(dev)} development images, reporting on {len(test)} test images")
    vp, vd, vy = score(args.data, dev)
    tp_, td, ty = score(args.data, test)

    gate = engine.gate
    d_thr = gate["distance_threshold"]
    t_pneu = gate.get("pneumonia_threshold",
                      max(gate["confidence_threshold"], 1 - gate["confidence_threshold"]))
    ind = vd <= d_thr
    lowest_pneu = float(vp[ind & (vy == 1)].min())
    t_norm = float(np.floor(lowest_pneu * 1e4) / 1e4)
    if t_norm >= lowest_pneu:
        t_norm -= 1e-4

    gate.update({"pneumonia_threshold": t_pneu, "normal_threshold": t_norm,
                 "gate_rule": f"pneumonia if p >= {t_pneu:.6f}; normal if p <= {t_norm:.4f}; "
                              "else escalate. normal_threshold: highest value clearing no "
                              "in-distribution pneumonia in the sel+cal splits (scripts/retune_gate.py); "
                              "all other parameters from the v2 notebook."})
    (ROOT / "models" / "gate_params.json").write_text(json.dumps(gate, indent=1))

    res_path = ROOT / "results" / "gating_results.json"
    res = json.loads(res_path.read_text())
    res.setdefault("notebook_symmetric_gate", {k: res[k] for k in
                   ("gated_accepted_only", "accepted", "escalated_low_confidence",
                    "rejected_out_of_distribution", "autonomy_rate", "final_states") if k in res})
    new_test = evaluate(tp_, td, ty, t_norm, t_pneu, d_thr)
    res.update({k: new_test[k] for k in ("accepted", "escalated_low_confidence",
                                         "rejected_out_of_distribution", "autonomy_rate",
                                         "final_states")})
    res["gated_accepted_only"] = {k: new_test[k] for k in
                                  ("sensitivity", "specificity", "precision", "npv", "accuracy",
                                   "f1", "tp", "tn", "fp", "fn")} | {"n": new_test["accepted"]}
    res["development_two_sided_gate"] = evaluate(vp, vd, vy, t_norm, t_pneu, d_thr)
    res_path.write_text(json.dumps(res, indent=2))
    print(json.dumps({"normal_threshold": t_norm, "pneumonia_threshold": t_pneu,
                      "test": new_test}, indent=1))


if __name__ == "__main__":
    main()
