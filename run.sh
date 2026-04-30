#!/bin/bash
# ============================================================
# Bybit Agentic Grid Trader — Launch Script
# ============================================================
cd "$(dirname "$0")"
PY="./venv/bin/python3"

case "${1:-multi}" in
  start)
    echo "🚀 Starting Single-Grid Agentic Trader..."
    $PY agentic_orchestrator.py
    ;;
  multi)
    echo "🚀 Starting Multi-Grid Agentic Trader (up to 20 concurrent grids)..."
    exec flock -n /tmp/grid_trader_multi.lock $PY multi_grid_manager.py
    ;;
  scan)
    echo "🔍 Running coin scan..."
    $PY -c "
import asyncio
from coin_scanner import CoinScanner

async def scan():
    s = CoinScanner()
    scores = await s.scan()
    print()
    print('=' * 80)
    print(f'{\"Rank\":<5} {\"Symbol\":<20} {\"Score\":<8} {\"Range%\":<8} {\"ATR%\":<8} {\"Grids\":<7} {\"Lev\":<5}')
    print('=' * 80)
    for i, c in enumerate(scores[:10]):
        print(f'{i+1:<5} {c.symbol:<20} {c.grid_score:<8.3f} {c.range_pct:<8.2f} {c.atr_pct:<8.2f} {c.suggested_grids:<7} {c.suggested_leverage}x')
    await s.close()

asyncio.run(scan())
"
    ;;
  stats)
    echo "📊 Trade Journal Statistics:"
    $PY -c "
from improvement_loop import ImprovementLoop
import json

j = ImprovementLoop()
stats = j.get_stats()
print(json.dumps(stats, indent=2, default=str))
suggestions = j.suggest_params()
print()
print('🧠 Suggestions:')
for r in suggestions.get('reason', []):
    print(f' → {r}')
print(f' Leverage: {suggestions.get(\"leverage\", \"N/A\")}x')
print(f' Grids: {suggestions.get(\"num_grids\", \"N/A\")}')
if 'preferred_symbol' in suggestions:
    print(f' Preferred: {suggestions[\"preferred_symbol\"]}')
"
    ;;
  test)
    echo "🔌 Testing Bybit API connection..."
    $PY -c "
import asyncio
from config import BYBIT_API_KEY, BYBIT_API_SECRET, TRADING_MODE
from coin_scanner import CoinScanner

async def test():
    if not BYBIT_API_KEY or BYBIT_API_KEY == 'your_api_key_here':
        print('❌ API keys not set! Edit .env file')
        return
    print(f'Mode: {TRADING_MODE}')
    print(f'Key: {BYBIT_API_KEY[:8]}...')
    s = CoinScanner()
    try:
        balance = await s.exchange.fetch_balance()
        usdt = balance.get('USDT', {})
        print(f'✅ Connected! USDT balance: free={usdt.get(\"free\",0)} total={usdt.get(\"total\",0)}')
        markets = await s.exchange.load_markets()
        linear = [m for m in markets.values() if m.get('linear') and '/USDT' in m['symbol']]
        print(f'✅ Markets loaded: {len(linear)} USDT linear perps available')
    except Exception as e:
        print(f'❌ Connection failed: {e}')
    await s.close()

asyncio.run(test())
"
    ;;
  *)
    echo "Usage: $0 {multi|start|scan|stats|test}"
    echo ""
    echo "  multi  — Run multi-grid mode (up to 20 concurrent grids with LLM) [default]"
    echo "  start  — Run single-grid agentic mode"
    echo "  scan   — Scan and rank coins"
    echo "  stats  — Show trade journal statistics"
    echo "  test   — Test API connection"
    ;;
esac
