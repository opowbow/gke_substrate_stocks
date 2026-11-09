import argparse
import contextlib
import json
import re
import socket
import subprocess
import time
import urllib.request

BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RED = "\033[31m"
RESET = "\033[0m"


def sh(cmd: str) -> str:
  return subprocess.check_output(cmd, shell=True, text=True).strip()


def banner(title: str):
  print(f"\n{BOLD}{'=' * 78}\n  {title}\n{'=' * 78}{RESET}")

def get_actor_worker_ip(actor_name: str, fallback_ip: str = "") -> str:
  out = sh("kubectl ate get actor -a trading-desk 2>/dev/null || true")
  for line in out.splitlines():
    if actor_name in line:
      ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line)
      if ips:
        return ips[0]
      for tok in line.split():
        if tok.startswith("stock-analyst-pool-"):
          return sh(f"kubectl get pod {tok} -n default -o jsonpath='{{.status.podIP}}' 2>/dev/null || true") or fallback_ip
  return fallback_ip


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


@contextlib.contextmanager
def actor_proxy(actor_name: str, atespace: str = "trading-desk", router_url: str = ""):
  """Connects via --router-url if given, else spawns `kubectl ate proxy actor`."""
  if router_url:
    yield router_url, {"ate-target-actor": f"{atespace}/{actor_name}"}
    return

  port = get_free_port()
  proc = subprocess.Popen(
      ["kubectl", "ate", "proxy", "actor", actor_name, f"{port}:8080", "-a", atespace],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
  )
  try:
    wait_for_local_port(port)
    yield f"http://127.0.0.1:{port}", {}
  finally:
    proc.terminate()
    try:
      proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
      proc.kill()


@contextlib.contextmanager
def pod_port_forward(pod_name: str, namespace: str = "default"):
  port = get_free_port()
  proc = subprocess.Popen(
      ["kubectl", "port-forward", f"pod/{pod_name}", f"{port}:8080", "-n", namespace],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
  )
  try:
    wait_for_local_port(port)
    yield f"http://127.0.0.1:{port}"
  finally:
    proc.terminate()
    try:
      proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
      proc.kill()


def call_actor_json(actor_name: str, path: str, router_url: str = "", method: str = "GET", body: dict = None):
  with actor_proxy(actor_name, router_url=router_url) as (base_url, extra_headers):
    headers = dict(extra_headers)
    data_bytes = None
    if body is not None:
      data_bytes = json.dumps(body).encode("utf-8")
      headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data_bytes,
        headers=headers,
        method=method,
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
      parsed = json.loads(resp.read().decode())
    elapsed_s = time.perf_counter() - t0
    return parsed, elapsed_s


def test_1_speed_risk(router_url: str = ""):
  banner("TEST 1 (THE SPEED RISK): Cold Regular GKE Pod vs. Warm Agent Substrate Worker")

  # --- 1A. Regular GKE Pod (Cold Start) ---
  print(f"\n{YELLOW}[Left Side: Regular GKE]{RESET} Creating Pod & waiting for /readyz...")
  sh("kubectl delete pod stock-analyst-cold-pod --ignore-not-found --wait=true >/dev/null 2>&1 || true")
  t0 = time.perf_counter()
  sh("kubectl apply -f manifests/standard-gke-pod.yaml >/dev/null")
  sh("kubectl wait --for=condition=Ready pod/stock-analyst-cold-pod --timeout=120s >/dev/null")
  cold_spinup_s = time.perf_counter() - t0
  print(f"  ⏱️  Regular GKE Pod Ready in: {BOLD}{YELLOW}{cold_spinup_s:.2f}s{RESET}")

  # --- 1B. Agent Substrate Actor on Warm WorkerPool ---
  print(f"\n{GREEN}[Right Side: Agent Substrate]{RESET} Waking Actor 'trader-alice' on WorkerPool for NVDA...")
  sh("kubectl ate create actor trader-alice -a trading-desk --template stock-analyst >/dev/null 2>&1 || true")
  sh("kubectl ate suspend actor trader-alice -a trading-desk >/dev/null 2>&1 || true")

  data, substrate_wake_s = call_actor_json("trader-alice", "/analyze?symbol=NVDA", router_url=router_url)
  state = data["session_state_in_ram"]
  worker_ip = get_actor_worker_ip("trader-alice", state["current_pod_ip"])

  print(
      f"  ⚡ Agent Substrate Wake + Analyze in: {BOLD}{GREEN}{substrate_wake_s * 1000:.0f}ms{RESET}"
      f" ({BOLD}{cold_spinup_s / substrate_wake_s:.1f}x faster{RESET})"
  )
  print(
      f"  📊 {BOLD}{data['symbol']}{RESET}: ${data['price']} | "
      f"30d Annualized Volatility: {BOLD}{data['annualized_volatility_pct']}% ({data['risk_rating']}){RESET}"
  )
  print(
      f"  🔒 Worker Pod IP: {CYAN}{worker_ip}{RESET} | "
      f"RAM Secret: {YELLOW}{state['resident_secret']}{RESET} | "
      f"Portfolio in RAM: {state['portfolio_history']}"
  )

  return worker_ip, state["resident_secret"]


def test_2_idle_risk(router_url: str = "", alice_first_ip: str = "", alice_secret: str = ""):
  banner("TEST 2 (THE IDLE RISK): Idle Suspend, 0ms Worker Reuse & Stateful Teleportation")

  # Ensure trader-alice has an initial NVDA session in RAM if Test 2 is run standalone
  if not alice_first_ip or not alice_secret:
    sh("kubectl ate create actor trader-alice -a trading-desk --template stock-analyst >/dev/null 2>&1 || true")
    init_data, _ = call_actor_json("trader-alice", "/analyze?symbol=NVDA", router_url=router_url)
    init_state = init_data["session_state_in_ram"]
    alice_first_ip = get_actor_worker_ip("trader-alice", init_state["current_pod_ip"])
    alice_secret = init_state["resident_secret"]

  print(f"\n{MAGENTA}[1. Alice Goes Idle]{RESET} Suspending 'trader-alice' RAM state to Cloud Storage...")
  t_susp = time.perf_counter()
  sh("kubectl ate suspend actor trader-alice -a trading-desk >/dev/null 2>&1")
  print(
      f"  💤 Alice suspended in {BOLD}{(time.perf_counter() - t_susp) * 1000:.0f}ms{RESET}. "
      f"Worker {CYAN}{alice_first_ip}{RESET} released back to WorkerPool (0 CPU / 0 RAM billed for Alice)."
  )

  print(f"\n{MAGENTA}[2. Bob Claims Freed Worker]{RESET} Waking 'trader-bob' on released WorkerPool slot...")
  sh("kubectl ate create actor trader-bob -a trading-desk --template stock-analyst >/dev/null 2>&1 || true")
  bob_data, bob_elapsed_s = call_actor_json("trader-bob", "/analyze?symbol=MSFT", router_url=router_url)
  bob_ip = get_actor_worker_ip("trader-bob", bob_data["session_state_in_ram"]["current_pod_ip"])
  print(
      f"  ⚡ Bob analyzed {BOLD}MSFT (${bob_data['price']}){RESET} in "
      f"{BOLD}{GREEN}{bob_elapsed_s * 1000:.0f}ms{RESET} on Worker IP {CYAN}{bob_ip}{RESET}"
  )

  print(f"\n{MAGENTA}[3. Alice Returns (Follow-Up on GOOGL)]{RESET} Waking 'trader-alice' while Bob holds {bob_ip}...")
  alice2_data, alice_resume_s = call_actor_json("trader-alice", "/analyze?symbol=GOOGL", router_url=router_url)
  s2 = alice2_data["session_state_in_ram"]
  alice_second_ip = get_actor_worker_ip("trader-alice", s2["current_pod_ip"])

  print(f"  ⚡ Alice resumed + analyzed {BOLD}GOOGL (${alice2_data['price']}){RESET} in {BOLD}{GREEN}{alice_resume_s * 1000:.0f}ms{RESET}")
  print(f"  ✨ {BOLD}STATEFUL TELEPORTATION PROOF:{RESET}")
  print(f"     • Previous Worker Pod IP: {CYAN}{alice_first_ip}{RESET}")
  print(f"     • Current Worker Pod IP:  {CYAN}{alice_second_ip}{RESET}")
  print(f"     • Resident RAM Secret:    {YELLOW}{s2['resident_secret']}{RESET} (Matches '{alice_secret}': {s2['resident_secret'] == alice_secret})")
  print(f"     • In-Memory Portfolio:    {GREEN}{s2['portfolio_history']}{RESET} (Both NVDA & GOOGL preserved in RAM without a DB!)")


def test_3_isolation_risk(router_url: str = ""):
  banner("TEST 3 (THE ISOLATION RISK): On-the-Fly Python Code & Host Kernel Probe")

  print(f"\n{CYAN}[Prompt Injection Simulation]{RESET} Trader submits on-the-fly Python quant formula (/run_quant_script):")
  print('  "Calculate NVDA Sharpe Ratio AND probe host kernel, /dev/kmsg, raw sockets & KSA token"')

  # 3A. Probe Regular GKE Pod if running
  pod_status = sh("kubectl get pod stock-analyst-cold-pod -o jsonpath='{.status.phase}' 2>/dev/null || true")
  if pod_status == "Running":
    with pod_port_forward("stock-analyst-cold-pod") as pod_url:
      payload = json.dumps({"symbol": "NVDA"}).encode("utf-8")
      req_pod = urllib.request.Request(
          f"{pod_url}/run_quant_script",
          data=payload,
          headers={"Content-Type": "application/json"},
          method="POST",
      )
      with urllib.request.urlopen(req_pod, timeout=30) as resp:
        pdata = json.loads(resp.read().decode())
      paudit = pdata["sandbox_audit"]
      print(f"\n  {YELLOW}[Left Side: Regular GKE Pod (runc)]{RESET}")
      print(f"  📈 Quant Output:         NVDA Sharpe Ratio = {pdata['sharpe_ratio']} ({pdata['exec_ms']}ms)")
      print(f"  ⚠️  Kernel Seen:          {RED}{paudit['isolation_boundary']} ({paudit['kernel_release']}){RESET}")
      print(f"  ⚠️  KSA Token Mounted:    {RED}{paudit['ksa_token_mounted']}{RESET}")

  # 3B. Probe Agent Substrate gVisor Sandbox
  sh("kubectl ate create actor trader-alice -a trading-desk --template stock-analyst >/dev/null 2>&1 || true")
  qdata, _ = call_actor_json(
      "trader-alice",
      "/run_quant_script",
      router_url=router_url,
      method="POST",
      body={"symbol": "NVDA"},
  )
  audit = qdata["sandbox_audit"]

  print(f"\n  {GREEN}[Right Side: Agent Substrate (gVisor Sandbox)]{RESET}")
  print(f"  📈 Quant Output:         NVDA Sharpe Ratio = {BOLD}{GREEN}{qdata['sharpe_ratio']}{RESET} ({qdata['exec_ms']}ms)")
  print(f"  🛡️  Sandbox Kernel Seen:  {GREEN}{audit['isolation_boundary']}{RESET}")
  print(f"  🔒 /dev/kmsg Probe:      {audit['kmsg_probe']}")
  print(f"  🔒 AF_PACKET Raw Socket: {audit['raw_socket_probe']}")
  print(f"  🔒 KSA Token Mounted:    {audit['ksa_token_mounted']}")


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description="Run Regular GKE vs. Agent Substrate tests.")
  parser.add_argument(
      "--test",
      choices=["1", "2", "3", "all"],
      default="all",
      help="Run Test 1 (speed), Test 2 (idle state), Test 3 (isolation), or all.",
  )
  parser.add_argument(
      "--router-url",
      default="",
      help="Optional router URL (if omitted, uses `kubectl ate proxy actor` automatically).",
  )
  args = parser.parse_args()

  first_ip, secret = "", ""
  if args.test in ("1", "all"):
    first_ip, secret = test_1_speed_risk(args.router_url)
  if args.test in ("2", "all"):
    test_2_idle_risk(args.router_url, first_ip, secret)
  if args.test in ("3", "all"):
    test_3_isolation_risk(args.router_url)
