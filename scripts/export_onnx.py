"""Export the deployed model to ONNX so the API can run without PyTorch.

Writes models/<arch>.onnx (outputs: post-ReLU feature map, logit, pooled embedding)
and models/<arch>.onnx.json (classifier weights for Grad-CAM and the SHA-256 of the
source .pt, so predictions keep the same model version string).

    python -m scripts.export_onnx
"""
import json

import torch

from backend.inference import MODEL_DIR, engine


class _Wrap(torch.nn.Module):
    def __init__(self, eng):
        super().__init__()
        self.model = eng.model
        self.eng = eng

    def forward(self, x):
        return self.eng._forward(x)


def main():
    if engine.demo or engine.arch != "densenet121":
        raise SystemExit("export supports the loaded densenet121 model only")
    out = MODEL_DIR / f"{engine.arch}.onnx"
    torch.onnx.export(_Wrap(engine).eval(), torch.zeros(1, 3, 224, 224), str(out),
                      input_names=["x"], output_names=["act", "logit", "emb"],
                      opset_version=17)
    fc = engine.model.classifier
    meta = {"arch": engine.arch, "source_weights_sha256": engine.weights_sha256,
            "classifier_weight": fc.weight.detach().numpy().ravel().tolist(),
            "classifier_bias": float(fc.bias.item())}
    (MODEL_DIR / f"{engine.arch}.onnx.json").write_text(json.dumps(meta))
    print("wrote", out)


if __name__ == "__main__":
    main()
