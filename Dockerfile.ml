# GPU-capable image for the behavioural anti-fraud ML subsystem (training + batch inference).
# Base already ships torch + CUDA, so the Autoencoder and GNN train on the GPU automatically
# (ml.hardware detects it). Run with `--gpus all` (needs the NVIDIA Container Toolkit on the
# host). CPU-only hosts can still use it — the code falls back to CPU.
FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# system deps for psycopg + plotting
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY ml/requirements-ml.txt /app/ml/requirements-ml.txt
# Single source of truth: the SAME pinned requirements the serving image uses, so the model is
# trained and served with identical library versions (no pickle/version skew). torch/numpy are
# already in the base image and are satisfied (not re-installed) by the pins.
RUN pip install --no-cache-dir -r /app/ml/requirements-ml.txt

COPY ml/ /app/ml/

# Defaults: reach the store over the compose network; override BF_PG_* / hyper-params via env.
ENV BF_PG_HOST=db BF_PG_PORT=5432 BF_ARTIFACTS_DIR=/app/artifacts

# Train by default; override the command to serve / evaluate.
#   docker build -f Dockerfile.ml -t adhere-bf-ml .
#   docker run --gpus all --env-file .env -v $PWD/artifacts:/app/artifacts adhere-bf-ml
ENTRYPOINT ["python", "-m", "ml.train"]
