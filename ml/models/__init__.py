"""Detectors: Isolation Forest, Autoencoder, GNN embeddings, and the ensemble scorer.

Every detector exposes the same tiny interface:
    fit(X) -> self
    score(X) -> np.ndarray in [0, 1]   (higher = more anomalous, comparable across detectors)
    save(dir) / load(dir)
    available -> bool                   (False if its optional deps are missing)
So the ensemble can blend whatever is present and renormalise the weights.
"""
