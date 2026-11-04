import json
import math
import os
import random
import socket
import string
import time
from agent import calculate_volatility, get_stock_quote, root_agent
from fastapi import FastAPI
from google.adk.runners import Runner
from google.genai import types as genai_types
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="GKE Stock & Volatility Analyst Agent")


def get_current_ip() -> str:
  try:
    return socket.gethostbyname(socket.gethostname())
  except Exception:
    return os.getenv("POD_IP", "unknown")


def run_sandbox_isolation_audit() -> dict:
  """Audits kernel isolation & escape vectors when executing untrusted code on the fly."""
  kernel_release = os.uname().release
  is_gvisor = kernel_release.startswith("4.4.")

  # Probe 1: Attempt to read host kernel ring buffer (/dev/kmsg)
  try:
    with open("/dev/kmsg", "r", encoding="utf-8") as f:
      f.read(16)
    kmsg_status = "EXPOSED (Read /dev/kmsg succeeded)"
  except Exception as e:
    kmsg_status = f"BLOCKED ({type(e).__name__}: {e})"

  # Probe 2: Attempt to open a raw packet socket (CAP_NET_RAW / host network sniff)
  try:
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    s.close()
    raw_sock_status = "EXPOSED (AF_PACKET raw socket opened)"
  except Exception as e:
    raw_sock_status = f"BLOCKED ({type(e).__name__}: {e})"

  # Probe 3: Check if a Kubernetes ServiceAccount token is mounted in the filesystem
  ksa_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
  ksa_mounted = os.path.exists(ksa_path)

  return {
      "kernel_release": kernel_release,
      "isolation_boundary": (
          "gVisor User-Space Kernel (runsc Sentry — Host Kernel Hidden)"
          if is_gvisor
          else f"Shared Host Node Linux Kernel ({kernel_release})"
      ),
      "is_sandboxed": is_gvisor,
      "kmsg_probe": kmsg_status,
      "raw_socket_probe": raw_sock_status,
      "ksa_token_mounted": ksa_mounted,
  }


# Pure in-memory session state (persisted across suspend/resume by gVisor RAM snapshots)
PORTFOLIO_STATE = {
    "resident_secret": "FOLIO-" + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=6)
    ),
    "boot_pod_ip": get_current_ip(),
    "last_pod_ip": None,
    "tickers_analyzed": [],
    "turn_count": 0,
}

runner = Runner(agent=root_agent, app_name="financial_analyst")


class ChatRequest(BaseModel):
  session_id: str = "trader-1"
  message: str


class QuantScriptRequest(BaseModel):
  symbol: str = "NVDA"
  code: str = (
      "returns = [0.018, -0.007, 0.024, 0.011, 0.015]\n"
      "mean_r = sum(returns) / len(returns)\n"
      "std_r = (sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)) ** 0.5\n"
      "sharpe_ratio = round((mean_r / std_r) * (252 ** 0.5), 2)"
  )


@app.get("/healthz")
@app.get("/readyz")
async def readyz():
  return {"status": "ready", "pod_ip": get_current_ip()}


@app.get("/analyze")
async def analyze_ticker(symbol: str = "NVDA"):
  """Fast deterministic endpoint for side-by-side spin-up & state-mobility benchmarking."""
  t0 = time.perf_counter()
  symbol = symbol.strip().upper()
  current_ip = get_current_ip()

  quote = json.loads(await get_stock_quote(symbol))
  vol = json.loads(await calculate_volatility(symbol))

  PORTFOLIO_STATE["turn_count"] += 1
  if symbol not in PORTFOLIO_STATE["tickers_analyzed"]:
    PORTFOLIO_STATE["tickers_analyzed"].append(symbol)

  previous_ip = PORTFOLIO_STATE["last_pod_ip"] or PORTFOLIO_STATE["boot_pod_ip"]
  PORTFOLIO_STATE["last_pod_ip"] = current_ip

  return {
      "symbol": symbol,
      "price": quote["price"],
      "recent_5d": quote["recent_5d"],
      "annualized_volatility_pct": vol["annualized_volatility_pct"],
      "risk_rating": vol["risk_rating"],
      "session_state_in_ram": {
          "turn_count": PORTFOLIO_STATE["turn_count"],
          "portfolio_history": PORTFOLIO_STATE["tickers_analyzed"],
          "resident_secret": PORTFOLIO_STATE["resident_secret"],
          "previous_pod_ip": previous_ip,
          "current_pod_ip": current_ip,
          "teleported_across_pods": previous_ip != current_ip,
      },
      "sandbox_audit": run_sandbox_isolation_audit(),
      "compute_ms": round((time.perf_counter() - t0) * 1000, 2),
  }


@app.post("/run_quant_script")
async def run_quant_script(req: QuantScriptRequest):
  """Executes an on-the-fly Python quant formula and audits the sandbox boundary."""
  t0 = time.perf_counter()
  local_scope = {"math": math, "symbol": req.symbol}
  exec(req.code, {"__builtins__": {"sum": sum, "len": len, "round": round, "min": min, "max": max, "abs": abs}}, local_scope)
  return {
      "symbol": req.symbol,
      "sharpe_ratio": local_scope.get("sharpe_ratio"),
      "exec_ms": round((time.perf_counter() - t0) * 1000, 2),
      "sandbox_audit": run_sandbox_isolation_audit(),
  }


@app.post("/chat")
async def chat(req: ChatRequest):
  """Full Google ADK + Gemini 2.5 Flash conversational endpoint."""
  current_ip = get_current_ip()
  PORTFOLIO_STATE["turn_count"] += 1
  previous_ip = PORTFOLIO_STATE["last_pod_ip"] or PORTFOLIO_STATE["boot_pod_ip"]
  PORTFOLIO_STATE["last_pod_ip"] = current_ip

  msg = genai_types.Content(
      role="user", parts=[genai_types.Part(text=req.message)]
  )
  reply_parts = []
  async for event in runner.run_async(
      user_id="trader", session_id=req.session_id, new_message=msg
  ):
    if hasattr(event, "content") and event.content:
      for part in event.content.parts:
        if getattr(part, "text", None) and not getattr(part, "thought", False):
          reply_parts.append(part.text)

  return {
      "response": "".join(reply_parts),
      "session_state_in_ram": {
          "turn_count": PORTFOLIO_STATE["turn_count"],
          "resident_secret": PORTFOLIO_STATE["resident_secret"],
          "previous_pod_ip": previous_ip,
          "current_pod_ip": current_ip,
          "teleported_across_pods": previous_ip != current_ip,
      },
      "sandbox_audit": run_sandbox_isolation_audit(),
  }


if __name__ == "__main__":
  port = int(os.getenv("PORT", "8080"))
  uvicorn.run(app, host="0.0.0.0", port=port)
