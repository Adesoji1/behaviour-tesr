"""
Detector — GNN embeddings (structural anomaly), GPU-aware and OPTIONAL.

Learns node embeddings on the bipartite customer -> counterparty graph (ml.pipeline.graph) with
an unsupervised GraphSAGE link-prediction objective. A customer whose structural position is
unusual (odd fan-out / counterparty mix / collector proximity) gets a high structural anomaly,
broadcast to that customer's transactions.

Honest scope (1.md): we only see one hop out, so this captures one-hop structure and
shared-counterparty rings, NOT multi-hop laundering. Embeddings are computed in BATCH here and
looked up at inference, so there is no per-request GNN cost.

Degrades cleanly: needs torch + torch_geometric; if absent, `available` is False and the graph
FEATURES (already in the feature matrix) still carry the graph signal.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from .. import config, hardware
from .base import ScoreNormalizer

log = logging.getLogger("ml.gnn")


def _pyg_available() -> bool:
    try:
        import torch_geometric  # noqa: F401
        return hardware.torch_available()
    except Exception:
        return False


class GNNDetector:
    name = "gnn"

    def __init__(self, params: dict | None = None):
        self.params = {**config.GNN, **(params or {})}
        self.available = _pyg_available()
        self.device = hardware.device()
        self.norm = ScoreNormalizer()
        self.customer_score_: dict[str, float] = {}   # customer_key -> [0,1] structural anomaly
        self.history: dict = {"train_loss": [], "val_loss": []}

    def fit(self, graph: dict) -> "GNNDetector":
        if not self.available:
            log.warning("gnn: torch_geometric not available — skipped (graph FEATURES still used)")
            return self
        import torch
        from torch_geometric.nn import SAGEConv
        n_nodes = len(graph["nodes"])
        if n_nodes == 0:
            self.available = False
            return self
        if n_nodes > self.params["max_nodes"]:
            log.warning("gnn: %d nodes > cap %d — training on a degree-sampled subgraph is TODO; "
                        "skipping to stay within the 6GB GPU", n_nodes, self.params["max_nodes"])
            self.available = False
            return self
        torch.manual_seed(self.params["seed"])
        dev = torch.device(self.device)
        ei_all = torch.tensor(graph["edge_index"], dtype=torch.long, device=dev)
        # hold out a fraction of edges for validation (link-prediction generalisation).
        E_all = ei_all.shape[1]
        vperm = torch.randperm(E_all, device=dev)
        nval = max(1, int(E_all * config.HOLDOUT_FRAC)) if E_all > 4 else 0
        val_e = ei_all[:, vperm[:nval]] if nval else ei_all[:, :0]
        train_e = ei_all[:, vperm[nval:]]
        # message passing uses TRAIN edges only, made undirected
        edge_index = torch.cat([train_e, train_e.flip(0)], dim=1)
        x = torch.randn(n_nodes, 8, device=dev)     # random node init (structure-only)

        class SAGE(torch.nn.Module):
            def __init__(self, d_in, d_h, d_out):
                super().__init__()
                self.c1 = SAGEConv(d_in, d_h); self.c2 = SAGEConv(d_h, d_out)

            def forward(self, x, ei):
                h = self.c1(x, ei).relu()
                return self.c2(h, ei)

        model = SAGE(8, self.params["hidden"], self.params["embed_dim"]).to(dev)
        opt = torch.optim.Adam(model.parameters(), lr=self.params["lr"])

        def _link_loss(z, edges):
            # link-prediction loss: real edges score high, random pairs low
            src, dst = edges
            k = edges.shape[1]
            pos = (z[src] * z[dst]).sum(-1)
            neg_dst = torch.randint(0, n_nodes, (k,), device=dev)
            neg = (z[src] * z[neg_dst]).sum(-1)
            return -(torch.log(torch.sigmoid(pos) + 1e-9).mean()
                     + torch.log(1 - torch.sigmoid(neg) + 1e-9).mean())

        log.info("gnn: training GraphSAGE on %s (%d nodes, %d train / %d val edges)",
                 self.device, n_nodes, train_e.shape[1], val_e.shape[1])
        best, best_state, wait = float("inf"), None, 0
        patience = self.params.get("patience", 5)
        for epoch in range(self.params["epochs"]):
            model.train(); opt.zero_grad()
            z = model(x, edge_index)
            loss = _link_loss(z, train_e)
            loss.backward(); opt.step()
            self.history["train_loss"].append(float(loss.item()))
            if val_e.shape[1]:
                model.eval()
                with torch.no_grad():
                    vl = float(_link_loss(model(x, edge_index), val_e).item())
                self.history["val_loss"].append(vl)
                if vl < best - 1e-6:                       # improved -> checkpoint the best weights
                    best, best_state, wait = vl, {k: v.detach().cpu().clone()
                                                  for k, v in model.state_dict().items()}, 0
                else:
                    wait += 1
                    if wait >= patience:                   # plateaued -> stop, keep best weights
                        log.info("gnn: early stop at epoch %d (best val %.5f)", epoch, best)
                        break
        if best_state:                                     # restore lowest-val-loss weights
            model.load_state_dict(best_state)
        model.eval()
        with torch.no_grad():
            z = model(x, edge_index).cpu().numpy()
        emb = z[:graph["n_customers"]]              # customer embeddings only
        # structural anomaly of a customer = Isolation Forest on the embeddings
        from sklearn.ensemble import IsolationForest
        iso = IsolationForest(n_estimators=200, random_state=42, n_jobs=-1).fit(emb)
        raw = -iso.score_samples(emb)
        self.norm.fit(raw)
        s = self.norm.transform(raw)
        self.customer_score_ = {k: float(v) for k, v in zip(graph["customer_keys"], s)}
        return self

    def score(self, customer_keys) -> np.ndarray:
        """Broadcast per-customer structural anomaly to each transaction's customer_key."""
        if not self.available or not self.customer_score_:
            return np.zeros(len(customer_keys))
        return np.array([self.customer_score_.get(str(k), 0.5) for k in customer_keys])

    def save(self, d: str | Path) -> None:
        if not self.available:
            return
        d = Path(d); d.mkdir(parents=True, exist_ok=True)
        (d / "gnn_scores.json").write_text(json.dumps(self.customer_score_))
        (d / "gnn_history.json").write_text(json.dumps(self.history))

    def load(self, d: str | Path) -> "GNNDetector":
        d = Path(d)
        p = d / "gnn_scores.json"
        if p.exists():
            self.customer_score_ = json.loads(p.read_text())
            self.available = True
        return self
