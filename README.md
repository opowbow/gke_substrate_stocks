# GKE Agent Substrate vs. Standard GKE: Share Price & Volatility War Room (`gke_substrate_stocks`)

A side-by-side benchmark and visual split-screen demo comparing **Standard GKE Sandboxed Pods (`runtimeClassName: gvisor`)**, **GKE Agent Sandbox (`SandboxWarmPool`)**, and **GKE Agent Substrate (`WorkerPool` + `ActorTemplate`)** using an identical **Google ADK Financial & Volatility Analyst Agent**.

---

## 🎯 What This Demo Shows

**Demo 1 — Session Spin-Up Race (`~380ms` vs. `~8.6s`):**
   - **Standard GKE (`kind: Pod`):** Cold-starts a dedicated gVisor Pod per session (Pod scheduling $\rightarrow$ container image pull/unpack $\rightarrow$ Python 3.12 + FastAPI + `google-adk` initialization), taking **8–12 seconds** before the first stock analysis can run.
   - **GKE Agent Substrate (`WorkerPool` + `ActorTemplate`):** Claims a warm gVisor worker Pod from a `WorkerPool` and restores a golden memory snapshot (`SNAPSHOT_CONTENT_SCOPE_FULL`) where Python, FastAPI, and `google-adk` are already initialized in RAM—waking and analyzing `NVDA` in **~380ms (>20× faster)**.
     
**Demo 2 — Idle Suspend, `0ms` Hardware Reuse & Stateful Teleportation:**
   - When Trader Alice pauses to read the `NVDA` chart, Agent Substrate snapshots her process RAM to Cloud Storage in **~96ms** and releases `Worker #1 (10.40.1.08)` back to the `WorkerPool` (**0 CPU / 0 MiB RAM billed while idle**).
   - Trader Bob requests `MSFT` and immediately lands on `Worker #1 (10.40.1.08)` with **0ms hardware provisioning**.
   - While Bob occupies `Worker #1`, Alice asks a follow-up on `GOOGL`. Agent Substrate restores Alice's memory snapshot onto `Worker #2 (10.40.1.12)` in **~390ms**—preserving her in-memory `resident_secret` (`FOLIO-...`) and `portfolio_history` (`['NVDA', 'GOOGL']`) across physical Pods **without an external database**.
     
**Demo3 — On-the-Fly Code Execution & Sandbox Isolation Audit (`/run_quant_script`):**
   - Executes a dynamic Python quant formula on the fly (computing `NVDA` Sharpe Ratio) while auditing kernel isolation (`os.uname().release`, `/dev/kmsg`, `AF_PACKET` raw sockets, and mounted KSA tokens)—proving that **Agent Substrate's gVisor sandbox (`SANDBOX_CLASS_GVISOR`)** traps untrusted code inside the `4.4.0` user-space Sentry kernel and hides the GKE node kernel.

---

## 📁 Repository Structure

```text
gke_substrate_stocks/
├── agent/
│   ├── agent.py                        # Google ADK Financial Analyst + get_stock_quote & calculate_volatility tools
│   ├── main.py                         # FastAPI server with pure in-memory PORTFOLIO_STATE & Pod IP tracking
│   ├── requirements.txt                # Python dependencies
│   └── Dockerfile                      # Container image used across all 3 GKE execution models
├── manifests/
│   ├── standard-gke-pod.yaml           # Model A: Standard GKE on-demand gVisor Pod
│   ├── sandbox-warmpool.yaml           # Model B: GKE Agent Sandbox (SandboxTemplate + SandboxWarmPool)
│   ├── substrate-workerpool.yaml       # Model C: GKE Agent Substrate WorkerPool (ate.dev/v1alpha1)
│   └── substrate-actor-template.yaml   # Model C: GKE Agent Substrate ActorTemplate (SNAPSHOT_CONTENT_SCOPE_FULL)
├── dashboard/
│   ├── index.html                      # Visual Split-Screen War Room UI (works standalone or with server.py)
│   └── server.py                       # Optional local FastAPI server bridging UI to live GKE cluster
└── side_by_side_race.py                # Terminal CLI side-by-side benchmark & teleportation proof script
```

---

## 🚀 Quickstart

### Option A: Preview the Visual Split-Screen Dashboard Immediately
You can open `dashboard/index.html` directly in any browser, or run the local dashboard server:

```bash
python3 dashboard/server.py
# Open http://localhost:8090
```

### Option B: Deploy & Run on a Live GKE Cluster

#### 1. Build and Push the Agent Image
```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"
export ZONE="us-central1-a"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/agent-demo/stock-analyst:v1"

gcloud artifacts repositories create agent-demo \
    --repository-format=docker \
    --location=${REGION} || true

gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet

cd agent
docker build -t ${IMAGE} .
docker push ${IMAGE}
cd ..
```

#### 2. Install Agent Substrate on GKE Standard
```bash
curl -sSL https://raw.githubusercontent.com/ai-on-gke/substrate-gke/main/install.sh | bash
```

#### 3. Deploy the `WorkerPool`, `ActorTemplate`, and Standard GKE Comparison Pod
```bash
export SUBSTRATE_VERSION=$(kubectl get ds -n ate-system -l app=atelet \
    -o jsonpath='{.items[0].metadata.labels.ate\.dev/substrate-version}')
export WORKER_IMAGE=$(kubectl get deploy -n ate-system \
    -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' | sed 's/ate-.*/ateom-gvisor:'"${SUBSTRATE_VERSION}"'/')
export BUCKET_NAME="ate-snapshots-${PROJECT_ID}-${ZONE}"

# Update image placeholder in manifests/standard-gke-pod.yaml
sed -i "s|IMAGE_PLACEHOLDER|${IMAGE}|g" manifests/standard-gke-pod.yaml

# Apply Agent Substrate WorkerPool
sed -e "s|IMAGE_PLACEHOLDER|${IMAGE}|g" \
    -e "s|WORKER_IMAGE_PLACEHOLDER|${WORKER_IMAGE}|g" \
    -e "s|SUBSTRATE_VERSION_PLACEHOLDER|${SUBSTRATE_VERSION}|g" \
    -e "s|BUCKET_PLACEHOLDER|${BUCKET_NAME}|g" \
    manifests/substrate-workerpool.yaml | kubectl apply -f -

# Create atespace and ActorTemplate
kubectl ate create atespace trading-desk || true
sed -e "s|IMAGE_PLACEHOLDER|${IMAGE}|g" \
    -e "s|BUCKET_PLACEHOLDER|${BUCKET_NAME}|g" \
    manifests/substrate-actor-template.yaml > /tmp/actor-template-resolved.yaml
kubectl ate create actor-template -f /tmp/actor-template-resolved.yaml
```

#### 4. Run the Side-by-Side Race
```bash
# Port-forward the Agent Substrate router in the background
kubectl port-forward -n ate-system svc/atenet-router 8080:80 &

# Run the CLI race benchmark
python3 side_by_side_race.py
```
