# Behavioural Anti-Fraud — Hosting Cost Estimate

**Prepared for:** Line Manager (Anita) · **Date:** 2026-07-30 · **Status:** Draft for Review , **By:** Adesoji

**Scope:** Hosting the Behavioural Anti-Fraud **ML inference service** and the **Behavioural Profile Service**, including periodic model **training** (GPU), request traffic, concurrency, and supporting infrastructure.

> **Important:** All figures below are **planning estimates** in **USD/month**, based on representative AWS-class cloud pricing (for example `af-south-1` or `eu-west-1`). Actual costs will vary depending on the cloud provider, region, reserved/spot commitments, operating system, storage configuration, and observed production traffic. Reserved instances and spot pricing typically reduce compute costs by **40–70%**.

---

# 1. Architecture Decision: One Server or Two?

## Recommendation

Use **two logical tiers** within the **same region and VPC**:

- CPU infrastructure for live inference and the Behavioural Profile Service
- GPU infrastructure only for periodic model retraining

This provides the best balance between cost, scalability, and latency.

The important point is that **the GPU is never part of the live scoring path.**

### Why?

The ML pipeline was intentionally designed so that GPU computation happens only during training.

- **Graph Neural Network (GNN)** embeddings are generated during offline training and stored for lookup during inference.
- The **Autoencoder** is a lightweight neural network whose forward pass completes in well under a millisecond on CPU.
- **Isolation Forest**, feature engineering, and behavioural profile lookups are entirely CPU-based.

As a result:

- Live `/score` requests never require GPU resources.
- GPU machines only need to exist while retraining is running.
- Retraining has **zero impact** on live request latency.

| Option | Layout | Inter-service Latency | GPU Utilisation | Verdict |
|---------|---------|----------------------|-----------------|---------|
| **A. Single Server** | Everything (GPU + inference + profile service) on one GPU VM | ~0 ms (localhost) | GPU idle ~99% of the day | Simple deployment but significantly more expensive |
| **B. Recommended** | CPU inference + profile service, GPU started only during retraining | **<1 ms** (same VPC/subnet) | GPU billed only during retraining | **Best balance of cost and performance** |
| **C. Hybrid / On-Prem** | Train on the existing RTX 2060 workstation, serve from cloud or on-prem | <1 ms if co-located | Existing GPU reused | Lowest infrastructure cost if on-prem operations are acceptable |

### Latency

Keep both services within the **same cloud region and VPC** (ideally the same subnet).

Under this deployment:

- service-to-service latency remains **sub-millisecond**
- retraining is completely offline
- GPU activity never affects `/score` response time

The primary deployment mistake to avoid is placing the two services in different regions.

---

# 2. Expected Production Traffic

Current production data:

| Metric | Value | Source |
|---------|-------|--------|
| Cached transactions | 1,598,308 over 91 days | `bp_transactions_cache` |
| Average throughput | ~17,600 transactions/day (~0.20 TPS) | Derived |
| Estimated peak | ~2 TPS (10× average) | Planning estimate |
| Capacity planning target | 50 TPS | Engineering headroom |
| Request/response size | ~1–2 KB | API contract |
| Monthly webhook traffic | ~1.2 GB | Derived |
| Measured `/score` latency | `<<LATENCY_MS>>` ms | Current build |
| Retraining schedule | Daily | Policy |
| Retraining duration | ~11 minutes | RTX 2060 benchmark |

This is currently a **low-throughput, latency-sensitive workload**.

Infrastructure is therefore sized primarily for:

- high availability
- operational resilience
- future growth

rather than raw compute capacity.

---

# 3. Estimated Monthly Hosting Costs

## Option B — Recommended

### CPU Serving + On-Demand GPU Training

| Item | Typical Specification | Estimated Monthly Cost |
|------|-----------------------|------------------------|
| Inference API (High Availability) | **2 × 2–4 vCPU / 4–8 GB** | **~$150–250** |
| Behavioural Profile Service | Co-located with inference API | Included |
| Managed PostgreSQL | 2 vCPU / 8 GB / 100 GB SSD | **~$70–110** |
| GPU Training (On Demand) | NVIDIA T4 (~15 hours/month) | **~$8** |
| Load Balancer, Backups & Network | ALB, snapshots, minimal egress | **~$20–40** |
| Monitoring & Logging | Cloud monitoring and alerting | **~$10–25** |

### Estimated Total

| Deployment | Estimated Monthly Cost |
|------------|-----------------------|
| On-demand pricing | **~$260–430/month** |
| Reserved CPU + Spot GPU | **~$220–330/month** |

> The lower end of the range reflects the current production workload, which averages only **0.2 TPS**. The higher end assumes larger CPU instances, Multi-AZ databases, and enterprise-grade high availability.

Daily retraining costs approximately **$8/month** because each run lasts only around **11 minutes**.

Even increasing retraining to **hourly** would raise GPU costs to only around **$60/month**, making retraining frequency a relatively small contributor to overall infrastructure cost.

---

## Option A — Single Always-On GPU Server

| Item | Estimated Monthly Cost |
|------|------------------------|
| Always-on GPU VM (T4-class) | **~$350–400** |
| Managed PostgreSQL | **~$70–110** |
| Load Balancer, Backups, Monitoring | **~$30–65** |

### Estimated Total

**~$450–575/month**

This is operationally simple but pays for a GPU that remains unused for approximately **99% of the day**.

---

## Option C — Hybrid / On-Prem

Using the existing RTX 2060 workstation for training.

| Item | Estimated Monthly Cost |
|------|------------------------|
| GPU Training | Existing hardware | Power only |
| Serving | Small cloud CPU instances or on-prem containers | **~$0–250** |
| Database | Self-managed or managed | **~$0–110** |

### Estimated Total

**~$50–360/month**

This option provides the lowest infrastructure cost because existing GPU hardware is reused.

However, the organisation becomes responsible for:

- system availability
- backups
- security patching
- hardware maintenance
- power and networking reliability

---

# 4. Concurrency and Capacity

Current traffic levels are modest.

Based on measured performance:

- average throughput is approximately **0.2 TPS**
- expected peak is approximately **2 TPS**
- infrastructure is intentionally sized for approximately **50 TPS**

The API should run using:

```
workers ≈ (2 × vCPU) + 1
```

For a 4-vCPU instance this is approximately **9 workers**.

The deployed model is:

- loaded once per worker
- cached in memory
- reused for every request

No model loading occurs during inference.

The Behavioural Profile database uses read-only pooled connections.

### High Availability

The recommended deployment uses **two API nodes**.

These are included primarily for:

- failover
- maintenance
- resilience

rather than additional throughput.

### Autoscaling

If production traffic grows significantly:

- scale inference nodes based on CPU utilisation (for example >60%)
- scale PostgreSQL independently
- scale GPU retraining independently

Since inference remains CPU-only, GPU infrastructure does not affect request latency.

---

# 5. Primary Cost Drivers

| Cost Driver | Impact | Recommended Control |
|-------------|--------|---------------------|
| Retraining frequency | GPU runtime | Daily retraining is sufficient for current workload |
| Always-on GPU | Highest infrastructure cost | Use on-demand GPU instances |
| Cross-region deployment | Increased latency and network charges | Keep all services in one region/VPC |
| Database growth | Storage costs | Continue using a 90–180 day rolling window |
| High Availability | Additional CPU instances | Use a single node for development and non-production |

---

# 6. Scalability

The current production workload of approximately **17,600 transactions/day** is relatively small.

Even if transaction volume increased by **10×**, the recommended CPU-based serving architecture would remain well within expected capacity.

Future infrastructure growth is more likely to be driven by:

- larger behavioural datasets
- increased database storage
- higher retraining frequency

rather than live inference.

---

# 7. Recommendation subject to your Approval based on your Expert Judgement

The recommended deployment is **Option B** though this could be reviewed by you should you have a superior thought.

Deploy:

- two CPU inference nodes (for high availability)
- the Behavioural Profile Service on the same infrastructure
- managed PostgreSQL
- on-demand GPU instances used only during scheduled retraining

This architecture delivers:

- sub-millisecond service-to-service latency
- no GPU dependency during live scoring
- lower operational cost
- straightforward horizontal scaling

For planning purposes, a realistic production budget is:

- **~$260–430/month** using on-demand infrastructure
- **~$220–330/month** using reserved CPU instances and spot GPU capacity

As production traffic grows, infrastructure can be scaled incrementally without redesigning the ML architecture.

---

> **Assumptions:** These estimates are based on representative 2026 AWS-class pricing and should be treated as planning figures rather than quotations. Actual costs will depend on the chosen cloud provider, deployment region, operating system, storage configuration, reserved-instance commitments, and observed production traffic. Compliance, data residency, premium support, and enterprise networking costs are not included.