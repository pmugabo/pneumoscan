"""PneumoScan inference engine.

Loads the model trained by notebooks/PneumoScan_training.ipynb together with the
frozen gate parameters, then for each uploaded image produces:

    calibrated probability -> out-of-distribution score -> triage state -> Grad-CAM

Triage states
    CLEARED          in distribution, confident, predicted normal
    FLAGGED_URGENT   in distribution, confident, predicted pneumonia
    ESCALATED        in distribution but confidence below the operating threshold
    REJECTED         outside the training distribution (wrong view, bad quality,
                     not a paediatric chest radiograph)

If no trained weights are present the engine starts in DEMO mode so the interface
still runs: it returns clearly labelled placeholder output instead of pretending
to have a model.
"""
from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
IMG_SIZE = 224
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torchvision import models as tvm
    TORCH = True
except ImportError:  # the UI still runs without torch installed
    TORCH = False


@dataclass
class TriageResult:
    state: str
    label: str
    probability: float          # calibrated P(pneumonia)
    confidence: float           # calibrated confidence in whichever class won
    ood_score: float
    reason: str
    heatmap_png: str | None     # base64 data URI
    model_version: str
    thresholds: dict
    demo_mode: bool

    def to_dict(self):
        return asdict(self)


class Engine:
    def __init__(self, model_dir: Path = MODEL_DIR):
        self.dir = Path(model_dir)
        self.demo = True
        self.model = None
        self.arch = "none"
        self.gate = {"temperature": 1.0, "confidence_threshold": 0.5,
                     "distance_threshold": None}
        self.means = None
        self.precision = None
        self._load()

    # ---------------------------------------------------------------- load
    def _load(self):
        gate_file = self.dir / "gate_params.json"
        stats_file = self.dir / "gate_stats.npz"
        if not (TORCH and gate_file.exists()):
            print("[engine] DEMO MODE — no gate_params.json or torch unavailable")
            return

        self.gate = json.loads(gate_file.read_text())
        self.arch = self.gate.get("selected_model", "resnet50")
        weights = self.dir / f"{self.arch}.pt"
        if not weights.exists():
            print(f"[engine] DEMO MODE — {weights.name} not found")
            return

        self.model = self._build(self.arch)
        state = torch.load(weights, map_location="cpu")
        self.model.load_state_dict(state)
        self.model.eval()

        if stats_file.exists():
            st = np.load(stats_file)
            self.means = st["class_means"]
            self.precision = st["precision"]

        self.demo = False
        print(f"[engine] loaded {self.arch}  T={self.gate['temperature']:.3f}  "
              f"conf>={self.gate['confidence_threshold']:.3f}  "
              f"dist<={self.gate['distance_threshold']:.1f}")

    @staticmethod
    def _build(arch: str):
        if arch == "resnet50":
            m = tvm.resnet50()
            m.fc = nn.Linear(m.fc.in_features, 1)
        elif arch == "densenet121":
            m = tvm.densenet121()
            m.classifier = nn.Linear(m.classifier.in_features, 1)
        elif arch == "efficientnet_b0":
            m = tvm.efficientnet_b0()
            m.classifier[1] = nn.Linear(m.classifier[1].in_features, 1)
        else:
            raise ValueError(f"unsupported architecture: {arch}")
        return m

    def _target_layer(self):
        return {"resnet50": lambda m: m.layer4[-1],
                "densenet121": lambda m: m.features[-1],
                "efficientnet_b0": lambda m: m.features[-1]}[self.arch](self.model)

    def _embed(self, x):
        if self.arch == "resnet50":
            return torch.flatten(nn.Sequential(*list(self.model.children())[:-1])(x), 1)
        if self.arch == "densenet121":
            f = torch.relu(self.model.features(x))
            return torch.flatten(F.adaptive_avg_pool2d(f, 1), 1)
        return torch.flatten(self.model.avgpool(self.model.features(x)), 1)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def preprocess(img: Image.Image):
        rgb = img.convert("RGB").resize((IMG_SIZE, IMG_SIZE), Image.BILINEAR)
        arr = np.asarray(rgb, dtype=np.float32) / 255.0
        arr = (arr - MEAN) / STD
        return rgb, arr.transpose(2, 0, 1)[None]

    def mahalanobis(self, feat: np.ndarray) -> float:
        if self.means is None:
            return 0.0
        feat = feat.astype(np.float64).ravel()
        return float(min(
            (feat - mu) @ self.precision @ (feat - mu) for mu in self.means))

    @staticmethod
    def overlay(rgb: Image.Image, cam: np.ndarray) -> str:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import cm
        heat = (cm.jet(cam)[:, :, :3] * 255).astype(np.uint8)
        blend = (0.6 * np.asarray(rgb) + 0.4 * heat).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(blend).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    # -------------------------------------------------------------- public
    def predict(self, img: Image.Image) -> TriageResult:
        if self.demo:
            return self._demo_result()

        rgb, batch = self.preprocess(img)
        x = torch.tensor(batch)

        target = self._target_layer()
        store = {}
        h1 = target.register_forward_hook(lambda m, i, o: store.__setitem__("act", o))
        h2 = target.register_full_backward_hook(
            lambda m, gi, go: store.__setitem__("grad", go[0]))

        logit = self.model(x)
        self.model.zero_grad()
        logit.backward()
        h1.remove(); h2.remove()

        T = self.gate["temperature"]
        prob = float(torch.sigmoid(logit / T).item())
        conf = prob if prob >= 0.5 else 1 - prob

        with torch.no_grad():
            feat = self._embed(x).numpy()
        dist = self.mahalanobis(feat)

        conf_thr = self.gate["confidence_threshold"]
        gate_conf = max(conf_thr, 1 - conf_thr)
        dist_thr = self.gate.get("distance_threshold")
        if dist_thr is None:
            dist_thr = float("inf")

        # ---- the gate ----
        if dist > dist_thr:
            state, reason = "REJECTED", (
                "This image is unlike the radiographs the model was trained on. "
                "Check that it is a paediatric chest X-ray of adequate quality, "
                "or send it for specialist review.")
            heat = None
        elif conf < gate_conf:
            state, reason = "ESCALATED", (
                "The model is not confident enough to decide this case. "
                "Route it to a specialist for review.")
            heat = None
        else:
            is_pneu = prob >= 0.5
            state = "FLAGGED_URGENT" if is_pneu else "CLEARED"
            reason = ("Findings consistent with pneumonia. Prioritise clinical "
                      "assessment and treatment." if is_pneu else
                      "No pneumonia detected. No specialist review required.")
            w = store["grad"].mean(dim=(2, 3), keepdim=True)
            cam = F.relu((w * store["act"]).sum(1, keepdim=True))
            cam = F.interpolate(cam, (IMG_SIZE, IMG_SIZE), mode="bilinear",
                                align_corners=False)[0, 0].detach().numpy()
            cam = (cam - cam.min()) / (cam.max() - cam.min() + 1e-8)
            heat = self.overlay(rgb, cam)

        return TriageResult(
            state=state,
            label="Pneumonia" if prob >= 0.5 else "Normal",
            probability=prob, confidence=conf, ood_score=dist, reason=reason,
            heatmap_png=heat, model_version=f"{self.arch}",
            thresholds={"confidence": gate_conf,
                        "distance": None if not np.isfinite(dist_thr) else dist_thr,
                        "temperature": T},
            demo_mode=False)

    def _demo_result(self) -> TriageResult:
        return TriageResult(
            state="ESCALATED", label="unavailable", probability=0.0,
            confidence=0.0, ood_score=0.0,
            reason=("DEMO MODE: no trained model is loaded, so no clinical output "
                    "is produced. Run the training notebook and copy "
                    "gate_params.json, gate_stats.npz and <model>.pt into models/."),
            heatmap_png=None, model_version="none",
            thresholds={}, demo_mode=True)

    def info(self) -> dict:
        """JSON-safe: non-finite thresholds become null rather than inf."""
        gate = {k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                for k, v in self.gate.items()}
        return {"demo_mode": self.demo, "architecture": self.arch, "gate": gate,
                "states": ["CLEARED", "FLAGGED_URGENT", "ESCALATED", "REJECTED"]}


engine = Engine()
