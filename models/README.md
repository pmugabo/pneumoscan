# Trained model files

Copy these here from the Kaggle notebook output (`/kaggle/working/pneumoscan/`):

- `gate_params.json` — selected architecture, temperature, both thresholds
- `gate_stats.npz` — class means and precision matrix for the Mahalanobis score
- `<architecture>.pt` — weights, e.g. `resnet50.pt`

Without them the API runs in DEMO mode: the interface works but returns no
clinical output, which is the correct behaviour for a system that is supposed
to refuse rather than guess.
