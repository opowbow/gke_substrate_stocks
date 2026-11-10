"""Live Split-Screen Dashboard Server for gke_substrate_stocks.

Zero external Python dependencies required (uses Python's built-in http.server).
Bridges all 3 tests in dashboard/index.html directly to your connected GKE
cluster (`kubectl` and `kubectl ate`), keeping persistent `kubectl ate proxy`
tunnels warm so you see true Agent Substrate wake latencies.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import pathlib
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DASHBOARD_HTML = pathlib.Path(__file__).parent / "index.html"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST_COLD_POD = REPO_ROOT / "manifests" / "standard-gke-pod.yaml"

_proxy_lock = threading.Lock()
_actor_proxies: dict[str, tuple[int, subprocess.Popen]] = {}
_last_alice_worker: str = ""
_last_alice_secret: str = ""


def sh(cmd: str, timeout: int = 120) -> str:
  return subprocess.check_output(
      cmd, shell=True, stderr=subprocess.DEVNULL, text=True, timeout=timeout
  ).strip()


def get_free_port() -> int:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def wait_for_local_port(port: int, timeout_s: float = 15.0):
  deadline = time.perf_counter() + timeout_s
  while time.perf_counter() < deadline:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
      s.settimeout(0.2)
      if s.connect_ex(("127.0.0.1", port)) == 0:
        return
    time.sleep(0.05)
  raise TimeoutError(f"Timed out waiting for local proxy on port {port}")


def ensure_actor_proxy(actor_name: str, atespace: str = "trading-desk", force_restart: bool = False) -> int:
  """Maintains a persistent warm `kubectl ate proxy actor` tunnel per actor."""
  with _proxy_lock:
    existing = _actor_proxies.get(actor_name)
    if existing and not force_restart and existing[1].poll() is None:
      return existing[0]
    if existing:
      existing[1].terminate()
      try:
        existing[1].wait(timeout=2)
      except Exception:
        existing[1].kill()

    port = get_free_port()
    proc = subprocess.Popen(
        ["kubectl", "ate", "proxy", "actor", actor_name, f"{port}:8080", "-a", atespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_local_port(port)
    _actor_proxies[actor_name] = (port, proc)
    return port


def call_actor_json(actor_name: str, path: str, method: str = "GET", body: dict = None):
  """Calls an actor over its warm proxy tunnel (retrying once if tunnel reset)."""
  for attempt in range(2):
    port = ensure_actor_proxy(actor_name, force_restart=(attempt > 0))
    headers = {}
    data_bytes = None
    if body is not None:
      data_bytes = json.dumps(body).encode("utf-8")
      headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data_bytes,
        headers=headers,
        method=method,
    )
    try:
      t0 = time.perf_counter()
      with urllib.request.urlopen(req, timeout=30) as resp:
        parsed = json.loads(resp.read().decode())
      elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
      return parsed, elapsed_ms
    except Exception:
      if attempt == 1:
        raise


def get_actor_worker_info(actor_name: str, fallback_ip: str = "") -> dict:
  """Returns {'ip': '10.12.x.x', 'pod': 'stock-analyst-pool-...'} from `kubectl ate get actor`."""
  try:
    out = sh("kubectl ate get actor -a trading-desk", timeout=15)
    for line in out.splitlines():
      cols = line.split()
      if len(cols) >= 6 and cols[1] == actor_name:
        pod_short = cols[4].split("/")[-1]
        worker_ip = cols[5]
        return {"ip": worker_ip, "pod": pod_short, "label": f"{worker_ip} ({pod_short})"}
  except Exception:
    pass
  return {"ip": fallback_ip or "unknown", "pod": "worker", "label": fallback_ip or "unknown"}


def get_cluster_status() -> dict:
  try:
    ctx = sh("kubectl config current-context", timeout=8)
    pods_json = json.loads(
        sh("kubectl get pods -n default -l workload=stock-analyst -o json", timeout=10)
    )
    workers = []
    for item in pods_json.get("items", []):
      name = item["metadata"]["name"]
      ip = item.get("status", {}).get("podIP", "pending")
      phase = item.get("status", {}).get("phase", "Unknown")
      workers.append({"name": name, "ip": ip, "phase": phase})
    if not workers:
      # Fallback: match by pod prefix
      all_pods = json.loads(sh("kubectl get pods -n default -o json", timeout=10))
      for item in all_pods.get("items", []):
        name = item["metadata"]["name"]
        if name.startswith("stock-analyst-pool-"):
          workers.append({
              "name": name,
              "ip": item.get("status", {}).get("podIP", "pending"),
              "phase": item.get("status", {}).get("phase", "Unknown"),
          })
    return {"live_cluster": bool(ctx), "context": ctx, "workers": workers}
  except Exception:
    return {"live_cluster": False, "context": "", "workers": []}


def call_cold_pod_json(path: str, method: str = "GET", body: dict = None) -> dict:
  port = get_free_port()
  proc = subprocess.Popen(
      ["kubectl", "port-forward", "pod/stock-analyst-cold-pod", f"{port}:8080", "-n", "default"],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
  )
  try:
    wait_for_local_port(port)
    headers = {}
    data_bytes = None
    if body is not None:
      data_bytes = json.dumps(body).encode("utf-8")
      headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data_bytes,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
      return json.loads(resp.read().decode())
  finally:
    proc.terminate()
    try:
      proc.wait(timeout=2)
    except Exception:
      proc.kill()


class DashboardHandler(BaseHTTPRequestHandler):

  def log_message(self, fmt, *args):
    pass

  def _send_json(self, payload: dict, status: int = 200):
    raw = json.dumps(payload).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  def do_GET(self):
    parsed = urllib.parse.urlparse(self.path)
    if parsed.path == "/api/cluster_status":
      self._send_json(get_cluster_status())
      return

    html_bytes = DASHBOARD_HTML.read_bytes()
    self.send_response(200)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    self.send_header("Content-Length", str(len(html_bytes)))
    self.end_headers()
    self.wfile.write(html_bytes)

  def do_POST(self):
    global _last_alice_worker, _last_alice_secret
    parsed = urllib.parse.urlparse(self.path)
    qs = urllib.parse.parse_qs(parsed.query)
    symbol = qs.get("symbol", ["NVDA"])[0].upper()

    try:
      # --- TEST 1A: Agent Substrate Wake + Analyze ---
      if parsed.path == "/api/test1/substrate":
        sh("kubectl ate create actor trader-alice -a trading-desk --template stock-analyst || true")
        # Warm the mTLS proxy tunnel FIRST so we measure pure Substrate wake time
        try:
          call_actor_json("trader-alice", "/readyz")
        except Exception:
          pass
        sh("kubectl ate suspend actor trader-alice -a trading-desk || true")

        data, wake_ms = call_actor_json("trader-alice", f"/analyze?symbol={symbol}")
        worker = get_actor_worker_info("trader-alice", data["session_state_in_ram"]["current_pod_ip"])
        _last_alice_worker = worker["label"]
        _last_alice_secret = data["session_state_in_ram"]["resident_secret"]
        self._send_json({
            "live_cluster": True,
            "wake_ms": wake_ms,
            "worker": worker,
            "data": data,
        })
        return

      # --- TEST 1B: Regular GKE Cold Pod Spin-Up + Analyze ---
      if parsed.path == "/api/test1/standard":
        sh("kubectl delete pod stock-analyst-cold-pod -n default --ignore-not-found --wait=true || true")
        t0 = time.perf_counter()
        sh(f"kubectl apply -f {MANIFEST_COLD_POD}")
        sh("kubectl wait --for=condition=Ready pod/stock-analyst-cold-pod -n default --timeout=120s")
        spinup_s = round(time.perf_counter() - t0, 2)
        pod_ip = sh("kubectl get pod stock-analyst-cold-pod -n default -o jsonpath='{.status.podIP}'")
        pod_data = call_cold_pod_json(f"/analyze?symbol={symbol}")
        self._send_json({
            "live_cluster": True,
            "spinup_s": spinup_s,
            "pod_ip": pod_ip,
            "data": pod_data,
        })
        return

      # --- TEST 2: Idle Suspend, Worker Reuse & Stateful Teleportation ---
      if parsed.path == "/api/test2":
        sh("kubectl ate suspend actor trader-bob -a trading-desk || true")
        if not _last_alice_worker or not _last_alice_secret:
          sh("kubectl ate suspend actor trader-alice -a trading-desk || true")
          sh("kubectl ate create actor trader-alice -a trading-desk --template stock-analyst || true")
          init_data, _ = call_actor_json("trader-alice", "/analyze?symbol=NVDA")
          w0 = get_actor_worker_info("trader-alice", init_data["session_state_in_ram"]["current_pod_ip"])
          _last_alice_worker = w0["label"]
          _last_alice_secret = init_data["session_state_in_ram"]["resident_secret"]

        first_worker = _last_alice_worker
        first_secret = _last_alice_secret

        # 1. Suspend Alice
        t_susp = time.perf_counter()
        sh("kubectl ate suspend actor trader-alice -a trading-desk")
        suspend_ms = round((time.perf_counter() - t_susp) * 1000, 0)

        # 2. Wake Bob on MSFT
        sh("kubectl ate create actor trader-bob -a trading-desk --template stock-analyst || true")
        try:
          call_actor_json("trader-bob", "/readyz")
          sh("kubectl ate suspend actor trader-bob -a trading-desk || true")
        except Exception:
          pass
        bob_data, bob_ms = call_actor_json("trader-bob", "/analyze?symbol=MSFT")
        bob_worker = get_actor_worker_info("trader-bob", bob_data["session_state_in_ram"]["current_pod_ip"])

        # 3. Wake Alice on follow-up ticker (GOOGL or NVDA)
        next_sym = "GOOGL" if symbol == "NVDA" else "NVDA"
        alice2_data, alice_resume_ms = call_actor_json("trader-alice", f"/analyze?symbol={next_sym}")
        alice2_worker = get_actor_worker_info("trader-alice", alice2_data["session_state_in_ram"]["current_pod_ip"])
        _last_alice_worker = alice2_worker["label"]

        self._send_json({
            "live_cluster": True,
            "suspend_ms": suspend_ms,
            "first_worker": first_worker,
            "bob_ms": bob_ms,
            "bob_worker": bob_worker,
            "bob_data": bob_data,
            "alice_resume_ms": alice_resume_ms,
            "alice2_worker": alice2_worker,
            "alice2_data": alice2_data,
            "secret_matches": alice2_data["session_state_in_ram"]["resident_secret"] == first_secret,
        })
        return

      # --- TEST 3: On-the-Fly Code Execution & Kernel Isolation Audit ---
      if parsed.path == "/api/test3":
        fly_body = {
            "symbol": symbol,
            "code": (
                "returns = [0.018, -0.024, 0.021, -0.014, 0.011]\n"
                "mean_r = sum(returns) / len(returns)\n"
                "sq_diff = 0.0\n"
                "for r in returns:\n"
                "  sq_diff += (r - mean_r) ** 2\n"
                "std_r = (sq_diff / (len(returns) - 1)) ** 0.5\n"
                "sharpe_ratio = round((mean_r / std_r) * (252 ** 0.5), 2)"
            ),
        }
        sh("kubectl ate create actor trader-alice -a trading-desk --template stock-analyst || true")
        try:
          sub_qdata, _ = call_actor_json(
              "trader-alice", "/run_fly_script", method="POST", body=fly_body
          )
        except urllib.error.HTTPError as e:
          if e.code == 404:
            sub_qdata, _ = call_actor_json(
                "trader-alice", "/run_quant_script", method="POST", body=fly_body
            )
          else:
            raise

        std_qdata = None
        pod_phase = sh("kubectl get pod stock-analyst-cold-pod -n default -o jsonpath='{.status.phase}' || true")
        if pod_phase == "Running":
          try:
            std_qdata = call_cold_pod_json("/run_fly_script", method="POST", body=fly_body)
          except urllib.error.HTTPError as e:
            if e.code == 404:
              std_qdata = call_cold_pod_json("/run_quant_script", method="POST", body=fly_body)
            else:
              raise

        if sub_qdata and "sandbox_audit" in sub_qdata:
          kr = sub_qdata["sandbox_audit"].get("kernel_release", "")
          if "gvisor" in kr.lower() or kr.startswith("4.4."):
            sub_qdata["sandbox_audit"]["is_sandboxed"] = True
            sub_qdata["sandbox_audit"]["isolation_boundary"] = (
                f"gVisor User-Space Kernel (runsc Sentry — Host Kernel Hidden) ({kr})"
            )

        self._send_json({
            "live_cluster": True,
            "substrate": sub_qdata,
            "standard": std_qdata,
        })
        return

      self._send_json({"error": "Not found"}, status=404)
    except Exception as exc:
      self._send_json({"live_cluster": False, "error": str(exc)}, status=200)


if __name__ == "__main__":
  port = int(os.getenv("DASHBOARD_PORT", "8090"))
  server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
  print(f"🚀 Live GKE War Room Dashboard running at http://localhost:{port}")
  print("   (In Cloud Shell: click Web Preview -> Change port -> 8090)")
  server.serve_forever()
