import json
import os
import random
import socket
import string
import time
from agent import calculate_volatility, get_stock_quote, root_agent
from fastapi import FastAPI
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="GKE Stock & Volatility Analyst Agent")


def get_current_ip() -> str:
  try:
    return socket.gethostbyname(socket.gethostname())
  except Exception:
    return os.getenv("POD_IP", "unknown")


# Pure in-memory session state (persisted across suspend/resume by gVisor RAM snapshots)
PORTFOLIO_STATE = {
    "resident_secret": None,
    "boot_pod_ip": get_current_ip(),
    "last_pod_ip": None,
    "tickers_analyzed": [],
    "turn_count": 0,
}

runner = InMemoryRunner(agent=root_agent, app_name="financial_analyst")


class ChatRequest(BaseModel):
  session_id: str = "trader-1"
  message: str


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

  if PORTFOLIO_STATE["resident_secret"] is None:
    PORTFOLIO_STATE["resident_secret"] = "FOLIO-" + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=6)
    )

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
      "compute_ms": round((time.perf_counter() - t0) * 1000, 2),
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
  }


if __name__ == "__main__":
  port = int(os.getenv("PORT", "8080"))
  uvicorn.run(app, host="0.0.0.0", port=port)
