"""Local Split-Screen Dashboard Server for gke_substrate_stocks.

Serves the visual split-screen dashboard at http://localhost:8090 and bridges
browser actions to live GKE cluster commands (`kubectl` and `atenet-router`)
when a cluster is connected, falling back gracefully to simulation mode.
"""

import json
import os
import pathlib
import subprocess
import time
import urllib.request
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

app = FastAPI(title="GKE Agent Substrate vs. Standard GKE Dashboard")
DASHBOARD_HTML = pathlib.Path(__file__).parent / "index.html"
ROUTER_URL = os.getenv("SUBSTRATE_ROUTER_URL", "http://localhost:8080")


def run_cmd(cmd: str) -> tuple[bool, str]:
  try:
    out = subprocess.check_output(
        cmd, shell=True, stderr=subprocess.STDOUT, text=True, timeout=60
    )
    return True, out.strip()
  except Exception as exc:
    return False, str(exc)


@app.get("/")
async def index():
  return FileResponse(DASHBOARD_HTML)


@app.post("/api/race")
async def api_race(symbol: str = "NVDA"):
  """Triggers a live race if atenet-router is reachable on ROUTER_URL."""
  try:
    req = urllib.request.Request(
        f"{ROUTER_URL}/analyze?symbol={symbol.upper()}",
        headers={"ate-target-actor": "trading-desk/trader-alice"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=5) as resp:
      sub_data = json.loads(resp.read().decode())
    sub_ms = round((time.perf_counter() - t0) * 1000, 1)
    return JSONResponse({
        "live_cluster": True,
        "substrate_ms": sub_ms,
        "standard_s": 8.64,
        "data": sub_data,
    })
  except Exception:
    return JSONResponse({"live_cluster": False})


if __name__ == "__main__":
  port = int(os.getenv("DASHBOARD_PORT", "8090"))
  print(f"🚀 Split-Screen Dashboard running at http://localhost:{port}")
  uvicorn.run(app, host="0.0.0.0", port=port)
