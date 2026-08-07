"""
Behavioural anti-fraud ML subsystem.

Unsupervised ensemble (GNN embeddings + Isolation Forest + Autoencoder) that scores each
transaction against the customer's learned behaviour. Behavioural detection only — NO AML
rules (a separate service owns those). See docs/anti-fraud-model.md for the full contract.

Pipeline stages (ml.train orchestrates 1-8):
  1 ingest   -> ml.pipeline.ingest
  2 validate -> ml.pipeline.validate
  3 clean    -> ml.pipeline.clean
  4 features -> ml.pipeline.features (+ ml.pipeline.graph)
  5 gnn      -> ml.models.gnn
  6 models   -> ml.models.isoforest, ml.models.autoencoder
  7 ensemble -> ml.models.ensemble
  8 threshold-> ml.codes
  eval       -> ml.eval (metrics + plots)
  9 serve    -> ml.serve
  registry   -> ml.registry
"""
__version__ = "0.1.0"
