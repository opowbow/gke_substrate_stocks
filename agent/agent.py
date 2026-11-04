from datetime import datetime, timedelta
import json
import math
import os
import statistics
import time
from typing import Any
from google.adk.agents.llm_agent import Agent as LLMAgent
import httpx

ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")
BASE_URL = "https://www.alphavantage.co/query"
CACHE_TTL = 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

BASE_PRICES = {
    "NVDA": 495.20,
    "GOOGL": 176.40,
    "MSFT": 418.25,
    "AAPL": 224.50,
    "AMZN": 198.50,
    "TSLA": 242.80,
    "META": 562.40,
    "JPM": 215.70,
}


def _generate_mock_series(symbol: str) -> dict[str, Any]:
  """Generates a realistic, deterministic 30-day price series for reliable demos."""
  base_price = BASE_PRICES.get(
      symbol, 100.0 + (sum(ord(c) for c in symbol) % 250)
  )
  time_series = {}
  current_date = datetime.now()
  current_price = base_price
  for i in range(30):
    date = current_date - timedelta(days=i)
    if date.weekday() >= 5:
      continue
    date_str = date.strftime("%Y-%m-%d")
    daily_change = (hash(date_str + symbol) % 400 - 200) / 10000
    current_price = current_price * (1 + daily_change)
    time_series[date_str] = {"4. close": f"{current_price:.2f}"}
  return {
      "Meta Data": {"2. Symbol": symbol},
      "Time Series (Daily)": time_series,
  }


async def fetch_stock_data(symbol: str) -> dict[str, Any]:
  symbol = symbol.strip().upper()
  now = time.time()
  if symbol in _cache and (now - _cache[symbol][0]) < CACHE_TTL:
    return _cache[symbol][1]

  if not ALPHAVANTAGE_API_KEY:
    data = _generate_mock_series(symbol)
  else:
    url = f"{BASE_URL}?function=TIME_SERIES_DAILY&symbol={symbol}&apikey={ALPHAVANTAGE_API_KEY}"
    try:
      async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        if "Time Series (Daily)" not in data:
          data = _generate_mock_series(symbol)
    except Exception:
      data = _generate_mock_series(symbol)

  _cache[symbol] = (now, data)
  return data


async def get_stock_quote(symbol: str) -> str:
  """Get the latest closing price and 5-day sparkline for a stock ticker."""
  data = await fetch_stock_data(symbol)
  ts = data["Time Series (Daily)"]
  dates = sorted(ts.keys(), reverse=True)
  latest_date = dates[0]
  recent_5d = [float(ts[d]["4. close"]) for d in reversed(dates[:5])]
  return json.dumps({
      "symbol": symbol.upper(),
      "date": latest_date,
      "price": float(ts[latest_date]["4. close"]),
      "recent_5d": recent_5d,
  })


async def calculate_volatility(symbol: str) -> str:
  """Calculate 30-day annualized volatility percentage for a stock ticker."""
  data = await fetch_stock_data(symbol)
  ts = data.get("Time Series (Daily)", {})
  prices = [float(ts[d]["4. close"]) for d in sorted(ts.keys())]
  if len(prices) < 2:
    return json.dumps({"symbol": symbol, "error": "Not enough data"})

  returns = [
      (prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))
  ]
  mean_ret = statistics.mean(returns)
  variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
  annualized_vol = round(math.sqrt(variance) * math.sqrt(252) * 100, 2)
  risk_rating = (
      "HIGH"
      if annualized_vol > 30
      else ("MODERATE" if annualized_vol > 18 else "LOW")
  )
  return json.dumps({
      "symbol": symbol.upper(),
      "annualized_volatility_pct": annualized_vol,
      "risk_rating": risk_rating,
  })


SYSTEM_INSTRUCTION = """You are a Senior Quantitative Financial Analyst running on GKE.
When asked about a stock ticker:
1. Call 'get_stock_quote' to retrieve the current share price.
2. Call 'calculate_volatility' to compute 30-day annualized volatility.
3. Provide a concise risk summary and reference any tickers analyzed earlier in this session.
"""

root_agent = LLMAgent(
    model="gemini-2.5-flash",
    name="financial_analyst",
    description="Analyzes stock share prices and 30-day annualized volatility.",
    instruction=SYSTEM_INSTRUCTION,
    tools=[get_stock_quote, calculate_volatility],
)
