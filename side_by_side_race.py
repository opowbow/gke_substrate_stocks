import json
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


def race_1_cold_pod_vs_substrate(router_url: str):
  banner("ACT 1: SESSION SPIN-UP RACE (Regular GKE Cold Pod vs. Agent Substrate)")

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

  req = urllib.request.Request(
      f"{router_url}/analyze?symbol=NVDA",
      headers={"ate-target-actor": "trading-desk/trader-alice"},
  )
  t1 = time.perf_counter()
  with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read().decode())
  substrate_wake_s = time.perf_counter() - t1

  state = data["session_state_in_ram"]
  print(
      f"  ⚡ Agent Substrate Wake + Analyze in: {BOLD}{GREEN}{substrate_wake_s * 1000:.0f}ms{RESET}"
      f" ({BOLD}{cold_spinup_s / substrate_wake_s:.1f}x faster{RESET})"
  )
  print(
      f"  📊 {BOLD}{data['symbol']}{RESET}: ${data['price']} | "
      f"30d Annualized Volatility: {BOLD}{data['annualized_volatility_pct']}% ({data['risk_rating']}){RESET}"
  )
  print(
      f"  🔒 Worker Pod IP: {CYAN}{state['current_pod_ip']}{RESET} | "
      f"RAM Secret: {YELLOW}{state['resident_secret']}{RESET} | "
      f"Portfolio in RAM: {state['portfolio_history']}"
  )
  return state["current_pod_ip"], state["resident_secret"]


def race_2_pod_reuse_and_teleportation(router_url: str, alice_first_ip: str, alice_secret: str):
  banner("ACT 2: IDLE SUSPEND, 0ms POD REUSE & STATEFUL TELEPORTATION")

  print(f"\n{MAGENTA}[1. Alice Pauses to Read Chart]{RESET} Suspending 'trader-alice' to Cloud Storage...")
  t_susp = time.perf_counter()
  sh("kubectl ate suspend actor trader-alice -a trading-desk >/dev/null")
  print(
      f"  💤 Alice suspended in {BOLD}{(time.perf_counter() - t_susp) * 1000:.0f}ms{RESET}. "
      f"Worker {CYAN}{alice_first_ip}{RESET} released back to WorkerPool (0 CPU / 0 RAM billed for Alice)."
  )

  print(f"\n{MAGENTA}[2. Bob Starts Session (MSFT)]{RESET} Waking 'trader-bob' on released WorkerPool slot...")
  sh("kubectl ate create actor trader-bob -a trading-desk --template stock-analyst >/dev/null 2>&1 || true")
  req_bob = urllib.request.Request(
      f"{router_url}/analyze?symbol=MSFT",
      headers={"ate-target-actor": "trading-desk/trader-bob"},
  )
  t_bob = time.perf_counter()
  with urllib.request.urlopen(req_bob, timeout=30) as resp:
    bob_data = json.loads(resp.read().decode())
  bob_ip = bob_data["session_state_in_ram"]["current_pod_ip"]
  print(
      f"  ⚡ Bob analyzed {BOLD}MSFT (${bob_data['price']}){RESET} in "
      f"{BOLD}{GREEN}{(time.perf_counter() - t_bob) * 1000:.0f}ms{RESET} on Worker IP {CYAN}{bob_ip}{RESET}"
  )

  print(f"\n{MAGENTA}[3. Alice Follow-Up (GOOGL)]{RESET} Waking 'trader-alice' while Bob holds {bob_ip}...")
  req_alice2 = urllib.request.Request(
      f"{router_url}/analyze?symbol=GOOGL",
      headers={"ate-target-actor": "trading-desk/trader-alice"},
  )
  t_alice2 = time.perf_counter()
  with urllib.request.urlopen(req_alice2, timeout=30) as resp:
    alice2_data = json.loads(resp.read().decode())
  alice_resume_ms = (time.perf_counter() - t_alice2) * 1000
  s2 = alice2_data["session_state_in_ram"]

  print(f"  ⚡ Alice resumed + analyzed {BOLD}GOOGL (${alice2_data['price']}){RESET} in {BOLD}{GREEN}{alice_resume_ms:.0f}ms{RESET}")
  print(f"  ✨ {BOLD}STATEFUL TELEPORTATION PROOF:{RESET}")
  print(f"     • Previous Worker Pod IP: {CYAN}{s2['previous_pod_ip']}{RESET}")
  print(f"     • Current Worker Pod IP:  {CYAN}{s2['current_pod_ip']}{RESET}")
  print(f"     • Resident RAM Secret:    {YELLOW}{s2['resident_secret']}{RESET} (Matches '{alice_secret}': {s2['resident_secret'] == alice_secret})")
  print(f"     • In-Memory Portfolio:    {GREEN}{s2['portfolio_history']}{RESET} (Both NVDA & GOOGL preserved in RAM without a DB!)")


def race_3_on_the_fly_code_isolation(router_url: str):
  banner("ACT 3: ON-THE-FLY CODE EXECUTION & SANDBOX ISOLATION AUDIT")

  print(f"\n{CYAN}[Prompt Injection Simulation]{RESET} Trader submits on-the-fly Python quant formula (/run_quant_script):")
  print('  "Calculate NVDA Sharpe Ratio AND probe host kernel, /dev/kmsg, raw sockets & KSA token"')

  payload = json.dumps({"symbol": "NVDA"}).encode("utf-8")
  req_quant = urllib.request.Request(
      f"{router_url}/run_quant_script",
      data=payload,
      headers={
          "Content-Type": "application/json",
          "ate-target-actor": "trading-desk/trader-alice",
      },
      method="POST",
  )
  with urllib.request.urlopen(req_quant, timeout=30) as resp:
    qdata = json.loads(resp.read().decode())

  audit = qdata["sandbox_audit"]
  print(f"\n  📈 {BOLD}On-the-Fly Quant Output:{RESET}  NVDA Sharpe Ratio = {BOLD}{GREEN}{qdata['sharpe_ratio']}{RESET} (executed in {qdata['exec_ms']}ms)")
  print(f"  🛡️  {BOLD}Sandbox Kernel Seen:{RESET}      {GREEN}{audit['isolation_boundary']}{RESET}")
  print(f"  🔒 {BOLD}/dev/kmsg Kernel Probe:{RESET}   {audit['kmsg_probe']}")
  print(f"  🔒 {BOLD}AF_PACKET Raw Socket:{RESET}     {audit['raw_socket_probe']}")
  print(f"  🔒 {BOLD}KSA Token Mounted:{RESET}        {audit['ksa_token_mounted']}")


if __name__ == "__main__":
  ROUTER_URL = "http://localhost:8080"
  first_ip, secret = race_1_cold_pod_vs_substrate(ROUTER_URL)
  race_2_pod_reuse_and_teleportation(ROUTER_URL, first_ip, secret)
  race_3_on_the_fly_code_isolation(ROUTER_URL)
