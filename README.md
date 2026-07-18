# Clarity Cloud — Algo Intelligence

All proprietary intelligence lives here. Never exposed.

## Three Engines

```
┌─────────────────────────────────────────┐
│         ENGINE 1: Decision Intelligence  │
│         Runs: Once daily                 │
│         Powered by: Alpha Vantage + FRED │
│         Output: Clarity Feed             │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│         ENGINE 2: Market Intelligence    │
│         Runs: Every candle (via Bridge)  │
│         Powered by: market_structure_    │
│                     engine_v3.py         │
│         Output: MarketSignal             │
│                 mood/attention/focus     │
│                 phase/BOS/CHOCH/POI      │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│         ENGINE 3: Execution Intelligence │
│         Runs: Every signal from Engine 2 │
│         Powered by: CALPA rules          │
│         Output: EXECUTE / ALERT / SKIP   │
└─────────────────────────────────────────┘
```

## Data Flow

```
Clarity Bridge (user's machine)
    ↓ streams candles + prices + account
WebSocket Server (api/server.py)
    ↓ updates BridgeProvider cache
Engine 2 (market_structure_engine_v3)
    ↓ MarketSignal
Engine 3 (CALPA)
    ↓ ExecutionDecision
    ├── SKIP   → nothing
    ├── ALERT  → push to Supabase → AlgoPage in Clarity UI
    └── EXECUTE→ send order back to Bridge → MT5 → Broker
                → auto-journal entry created in Supabase
```

## Directory Structure

```
clarity-cloud/
│
├── api/
│   └── server.py              ← WebSocket server (deploy on Railway/VPS)
│
├── providers/
│   ├── base.py                ← Provider interfaces
│   ├── bridge_provider.py     ← Receives data from Bridge
│   └── replay_provider.py     ← For backtesting
│
├── engines/
│   ├── market/
│   │   ├── market_structure_engine_v3.py  ← THE engine (copied from clarity-platform)
│   │   ├── adapter.py                     ← Bridges provider to engine
│   │   └── analyser.py                    ← Fallback if engine not found
│   │
│   └── execution/
│       └── calpa.py           ← CALPA rules (Engine 3)
│
└── core/
    ├── config.py              ← All settings
    └── signal.py              ← MarketSignal + ExecutionDecision dataclasses
```

## Setup — Engine 2

```bash
# Copy the real engine from clarity-platform
cp ../clarity-platform/core/market_structure_engine_v3.py \
   engines/market/market_structure_engine_v3.py
```

## Deploy

```bash
# Install dependencies
pip install websockets httpx supabase

# Set environment variables
export SUPABASE_URL=your_supabase_url
export SUPABASE_SERVICE_KEY=your_service_key
export CLARITY_BRIDGE_SECRET=your_secret
export PORT=8765

# Run the WebSocket server
python api/server.py
```

## Supabase Tables Required

```sql
-- Signals pushed to UI
create table algo_signals (
  id          uuid default gen_random_uuid() primary key,
  user_id     uuid references auth.users,
  pair        text,
  phase       text,
  mood        text,
  attention   text,
  focus       text,
  direction   text,
  action      text,
  reason      text,
  entry       float,
  sl          float,
  tp          float,
  sl_pips     float,
  rr          float,
  session     text,
  created_at  timestamptz default now()
);

-- Bridge connection status
create table algo_status (
  user_id         uuid references auth.users primary key,
  connected       boolean default false,
  broker          text,
  pairs_monitored int,
  engine2_running boolean default false,
  engine3_running boolean default false,
  mt5_connected   boolean default false,
  last_update     timestamptz default now()
);
```

## IP Protection

The intelligence never leaves Clarity Cloud.
Users install only the Bridge (dumb pipe).
The Bridge has no strategy, no analysis, no CALPA rules.
If a user reverse-engineers the Bridge, they get a WebSocket client. Nothing more.
