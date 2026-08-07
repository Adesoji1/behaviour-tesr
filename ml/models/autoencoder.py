"""
Detector — Autoencoder (reconstruction-error anomaly detection), GPU-aware.

Learns to reconstruct the customers' NORMAL deviation vectors. A transaction the network
cannot reconstruct well (high MSE) is behaviourally unusual. Trains on GPU if available (see
ml.hardware), otherwise CPU. Records per-epoch train/val loss for the training-curve plot.

Degrades cleanly: if torch is not installed, `available` is False and the orchestrator skips it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from .. import config, hardware
from .base import ScoreNormalizer

log = logging.getLogger("ml.autoencoder")


class AutoencoderDetector:
    name = "autoencoder"

    def __init__(self, params: dict | None = None):
        self.params = {**config.AUTOENCODER, **(params or {})}
        self.available = hardware.torch_available()
        self.model = None
        self.scaler = None
        self.norm = ScoreNormalizer()
        self.history: dict = {"train_loss": [], "val_loss": []}
        self.device = hardware.device()

    # ---- model definition (built lazily so torch import stays optional) -------
    def _build(self, n_in: int):
        import torch.nn as nn
        dims = [n_in] + list(self.params["hidden"])
        layers = []
        for a, b in zip(dims[:-1], dims[1:]):
            layers += [nn.Linear(a, b), nn.ReLU()]
        layers += [nn.Linear(dims[-1], n_in)]      # reconstruct to input dim
        return nn.Sequential(*layers)

    def fit(self, X: np.ndarray) -> "AutoencoderDetector":
        if not self.available:
            log.warning("autoencoder: torch not available — skipped")
            return self
        import torch
        from sklearn.preprocessing import RobustScaler
        torch.manual_seed(self.params["seed"]); np.random.seed(self.params["seed"])

        X = np.asarray(X, dtype=np.float32)
        self.scaler = RobustScaler().fit(X)
        Xs = self.scaler.transform(X).astype(np.float32)

        n = len(Xs); nval = max(1, int(n * self.params["val_frac"]))
        perm = np.random.permutation(n)
        val_idx, tr_idx = perm[:nval], perm[nval:]
        dev = torch.device(self.device)
        Xtr = torch.tensor(Xs[tr_idx], device=dev)
        Xval = torch.tensor(Xs[val_idx], device=dev)

        self.model = self._build(Xs.shape[1]).to(dev)
        opt = torch.optim.Adam(self.model.parameters(), lr=self.params["lr"],
                               weight_decay=self.params["weight_decay"])
        loss_fn = torch.nn.MSELoss()
        bs = self.params["batch_size"]
        best, best_state, wait = float("inf"), None, 0
        log.info("autoencoder: training on %s, %d train / %d val rows", self.device, len(tr_idx), len(val_idx))
        for epoch in range(self.params["epochs"]):
            self.model.train()
            idx = torch.randperm(len(Xtr), device=dev)
            tot = 0.0
            for i in range(0, len(Xtr), bs):
                b = Xtr[idx[i:i + bs]]
                opt.zero_grad()
                loss = loss_fn(self.model(b), b)
                loss.backward(); opt.step()
                tot += loss.item() * len(b)
            tr_loss = tot / len(Xtr)
            self.model.eval()
            with torch.no_grad():
                vl = loss_fn(self.model(Xval), Xval).item()
            self.history["train_loss"].append(tr_loss)
            self.history["val_loss"].append(vl)
            if vl < best - 1e-6:
                best, best_state, wait = vl, {k: v.detach().cpu().clone()
                                              for k, v in self.model.state_dict().items()}, 0
            else:
                wait += 1
                if wait >= self.params["patience"]:
                    log.info("autoencoder: early stop at epoch %d (val %.5f)", epoch, best)
                    break
        if best_state:
            self.model.load_state_dict(best_state)
        # calibrate normaliser on training reconstruction error
        self.norm.fit(self._recon_error(Xs))
        return self

    def _recon_error(self, Xs: np.ndarray) -> np.ndarray:
        import torch
        dev = torch.device(self.device)
        self.model.eval()
        errs = []
        with torch.no_grad():
            for i in range(0, len(Xs), 4096):
                b = torch.tensor(Xs[i:i + 4096], device=dev)
                errs.append(((self.model(b) - b) ** 2).mean(dim=1).cpu().numpy())
        return np.concatenate(errs) if errs else np.zeros(len(Xs))

    def score(self, X: np.ndarray) -> np.ndarray:
        if not self.available or self.model is None:
            return np.zeros(len(X))
        Xs = self.scaler.transform(np.asarray(X, dtype=np.float32)).astype(np.float32)
        return self.norm.transform(self._recon_error(Xs))

    def save(self, d: str | Path) -> None:
        if not self.available or self.model is None:
            return
        import torch
        d = Path(d); d.mkdir(parents=True, exist_ok=True)
        import joblib
        torch.save(self.model.state_dict(), d / "autoencoder.pt")
        joblib.dump({"scaler": self.scaler, "params": self.params}, d / "autoencoder_meta.joblib")
        (d / "autoencoder_norm.json").write_text(json.dumps(self.norm.to_dict()))
        (d / "autoencoder_history.json").write_text(json.dumps(self.history))

    def load(self, d: str | Path, n_features: int) -> "AutoencoderDetector":
        if not self.available:
            return self
        import torch, joblib
        d = Path(d)
        meta = joblib.load(d / "autoencoder_meta.joblib")
        self.scaler, self.params = meta["scaler"], meta["params"]
        self.model = self._build(n_features).to(self.device)
        self.model.load_state_dict(torch.load(d / "autoencoder.pt", map_location=self.device))
        self.norm = ScoreNormalizer.from_dict(json.loads((d / "autoencoder_norm.json").read_text()))
        return self
