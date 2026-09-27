"""
Solana memecoin trader - PAPER MODE  (Step 2 of the trading bot)

What it does:
  1. Runs the scanner (sol_scanner.py) every few minutes to find coins that
     passed the scam/rug checks.
  2. Before each buy, asks Jupiter for a real SOL -> token quote AND a
     token -> SOL quote for the same amount. If the coin can't be sold, or the
     round trip loses too much (hidden taxes, thin pools), it's skipped.
  3. "Buys" with fake SOL at the real Jupiter quote. Open positions are valued
     by what Jupiter would actually pay to sell them right now.
  4. Sells on take-profit, stop-loss, trailing stop, max hold time, or a
     liquidity pull (rug).
  5. Enforces spending caps: SOL per trade, SOL in play at once.
  6. Stops for the day when the daily profit target or loss limit is hit
     (remembered across restarts in paper_state.json).
  7. Winds down - sells everything back to SOL - when you press Ctrl+C or
     the session time runs out.

Every trade is logged to paper_trades.csv. No wallet, no keys, no real money.

Run until Ctrl+C:
    python trader.py
Run for 2 hours, or until 23:00:
    python trader.py --hours 2
    python trader.py --until 23:00
Start the paper wallet over:
    python trader.py --reset
"""

import argparse
import csv
import json
import os
import time
from datetime import date, datetime, timedelta

import requests

import sol_scanner as scanner

# ─────────────────────────────────────────────────────────────
# SETTINGS - tweak these to change how the bot trades
# ─────────────────────────────────────────────────────────────
SETTINGS = {
    # Paper wallet
    "starting_balance_sol": 5.0,

    # Spending caps
    "sol_per_trade": 0.25,           # size of each buy
    "max_sol_in_play": 1.0,          # total SOL in open positions at once
    "max_open_positions": 4,

    # Daily limits (realized + open P&L, in SOL, since midnight)
    "daily_profit_target_sol": 0.5,  # hit this -> sell everything, stop for today
    "daily_loss_limit_sol": 0.3,     # lose this -> sell everything, stop for today

    # Entry rules
    "buy_buckets": ["VOLATILE", "HIGH_VOLUME"],
    "min_chg_5m_pct": 0.0,           # only buy if it's rising right now (no falling knives)
    "min_chg_1h_pct": -20,           # skip coins crashing over the last hour (dead-cat bounces)
    "max_chg_1h_pct": 150,           # skip coins that already pumped too far in 1h
    "max_roundtrip_loss_pct": 5,     # buy-then-sell-straight-back loses more than this -> skip
    "cooldown_min": 60,              # don't re-buy a coin within this long of selling it

    # Exit rules (all measured on what you'd really get back, after costs)
    "take_profit_pct": 30,
    "stop_loss_pct": 15,
    "trailing_stop_pct": 12,         # sell if value falls this far from its peak...
    "trailing_arm_pct": 10,          # ...once it's been at least this far in profit
    "max_hold_min": 90,
    "rug_liquidity_drop_pct": 50,    # liquidity falls this much since entry -> get out

    # Trading costs
    "slippage_bps": 100,             # slippage tolerance you'd send with a live swap (1%)
    "network_fee_sol": 0.0005,       # priority fee + tx fee, each way (not in Jupiter quotes)

    # Timing
    "scan_every_min": 5,
    "check_prices_every_sec": 15,    # shorter = less stop-loss overshoot, more API calls
    "missing_price_writeoff": 20,    # checks in a row with no sell quote -> assume dead, value 0 (~5 min)
}

JUPITER_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
SOL = scanner.SOL_MINT
LAMPORTS = 1_000_000_000

STATE_FILE = "paper_state.json"
TRADES_FILE = "paper_trades.csv"
TRADE_COLS = [
    "time", "side", "symbol", "address", "reason", "price_usd", "sol_usd",
    "sol_amount", "tokens_raw", "pnl_sol", "pnl_pct", "held_min", "balance_sol",
]


# ─────────────────────────────────────────────────────────────
# STATE (persists the paper balance and today's P&L across runs)
# ─────────────────────────────────────────────────────────────
def load_state(reset=False):
    today = date.today().isoformat()
    state = {"balance_sol": SETTINGS["starting_balance_sol"], "day": today,
             "day_pnl_sol": 0.0, "day_stopped": False}
    if not reset and os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    if state["day"] != today:  # new day: reset the daily counters
        state.update(day=today, day_pnl_sol=0.0, day_stopped=False)
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def log_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


# ─────────────────────────────────────────────────────────────
# PRICES
# ─────────────────────────────────────────────────────────────
def jupiter_quote(input_mint, output_mint, amount_raw, retries=3):
    """Real swap quote from Jupiter. Returns (out_amount_raw, None) or (None, why)."""
    params = {"inputMint": input_mint, "outputMint": output_mint,
              "amount": int(amount_raw), "slippageBps": SETTINGS["slippage_bps"]}
    for attempt in range(retries):
        try:
            r = scanner.session.get(JUPITER_QUOTE, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 400:  # no route / not tradable
                return None, (r.json().get("error") or "no route")[:80]
            r.raise_for_status()
            return int(r.json()["outAmount"]), None
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == retries - 1:
                return None, f"quote failed ({type(e).__name__})"
            time.sleep(2 * (attempt + 1))
    return None, "rate limited"


def sol_price_usd():
    pairs = scanner.fetch_market_data([SOL])
    p = pairs.get(SOL)
    return float(p["priceUsd"]) if p and p.get("priceUsd") else None


# ─────────────────────────────────────────────────────────────
# TRADER
# ─────────────────────────────────────────────────────────────
class PaperTrader:
    def __init__(self, state):
        self.state = state
        self.positions = {}   # address -> position dict
        self.cooldown = {}    # address -> datetime it was last sold or skipped
        self.sol_usd = None

    # ── helpers ──────────────────────────────────────────────
    def in_play_sol(self):
        return sum(p["cost_sol"] for p in self.positions.values())

    def open_pnl_sol(self):
        return sum(p["value_sol"] - p["cost_sol"] for p in self.positions.values())

    def day_pnl_sol(self):
        return self.state["day_pnl_sol"] + self.open_pnl_sol()

    def skip(self, t, why):
        print(f"  SKIP {t['symbol'][:10]:<10} {why}")
        self.cooldown[t["address"]] = datetime.now()

    # ── buying ───────────────────────────────────────────────
    def buy(self, t):
        s = SETTINGS
        spend = s["sol_per_trade"]
        swap_lamports = int((spend - s["network_fee_sol"]) * LAMPORTS)

        tokens_raw, err = jupiter_quote(SOL, t["address"], swap_lamports)
        if not tokens_raw:
            return self.skip(t, f"no buy quote: {err}")

        # Honeypot / tax check: can we sell straight back, and at what loss?
        back_lamports, err = jupiter_quote(t["address"], SOL, tokens_raw)
        if not back_lamports:
            return self.skip(t, f"CAN'T SELL - honeypot? ({err})")
        roundtrip_loss = (1 - back_lamports / swap_lamports) * 100
        if roundtrip_loss > s["max_roundtrip_loss_pct"]:
            return self.skip(t, f"round trip loses {roundtrip_loss:.1f}% (tax or thin pool)")

        value = max(back_lamports / LAMPORTS - s["network_fee_sol"], 0.0)
        self.positions[t["address"]] = {
            "symbol": t["symbol"], "address": t["address"], "tokens_raw": tokens_raw,
            "cost_sol": spend, "value_sol": value, "peak_value_sol": value,
            "entry_price": t["price_usd"], "price": t["price_usd"],
            "entry_liq": t["liquidity_usd"], "liq": t["liquidity_usd"],
            "opened": datetime.now(), "misses": 0,
        }
        self.state["balance_sol"] -= spend
        save_state(self.state)

        print(f"  BUY  {t['symbol'][:10]:<10} {spend:.3f} SOL @ ${t['price_usd']:.8g}  "
              f"({t['bucket']}, 5m {t['chg_5m']:+.1f}%, 1h {t['chg_1h']:+.1f}%, "
              f"round trip -{roundtrip_loss:.1f}%)")
        log_trade({
            "time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "side": "BUY",
            "symbol": t["symbol"], "address": t["address"], "reason": t["bucket"],
            "price_usd": t["price_usd"], "sol_usd": round(self.sol_usd, 2),
            "sol_amount": spend, "tokens_raw": tokens_raw, "pnl_sol": "", "pnl_pct": "",
            "held_min": "", "balance_sol": round(self.state["balance_sol"], 4),
        })

    def pick_and_buy(self, candidates):
        s = SETTINGS
        now = datetime.now()
        for t in candidates:
            if len(self.positions) >= s["max_open_positions"]:
                break
            if self.in_play_sol() + s["sol_per_trade"] > s["max_sol_in_play"] + 1e-9:
                break
            if self.state["balance_sol"] < s["sol_per_trade"]:
                print("  (paper balance too low to buy)")
                break
            addr = t["address"]
            if addr in self.positions:
                continue
            last = self.cooldown.get(addr)
            if last and now - last < timedelta(minutes=s["cooldown_min"]):
                continue
            if t["bucket"] not in s["buy_buckets"]:
                continue
            if t["chg_5m"] < s["min_chg_5m_pct"]:
                continue
            if not s["min_chg_1h_pct"] <= t["chg_1h"] <= s["max_chg_1h_pct"]:
                continue
            self.buy(t)

    # ── selling ──────────────────────────────────────────────
    def sell(self, addr, reason):
        pos = self.positions.pop(addr)
        proceeds = pos["value_sol"]
        pnl = proceeds - pos["cost_sol"]
        pnl_pct = pnl / pos["cost_sol"] * 100
        held = (datetime.now() - pos["opened"]).total_seconds() / 60

        self.state["balance_sol"] += proceeds
        self.state["day_pnl_sol"] += pnl
        save_state(self.state)
        self.cooldown[addr] = datetime.now()

        print(f"  SELL {pos['symbol'][:10]:<10} {proceeds:.4f} SOL  "
              f"P&L {pnl:+.4f} SOL ({pnl_pct:+.1f}%)  held {held:.0f}m  [{reason}]")
        log_trade({
            "time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "side": "SELL",
            "symbol": pos["symbol"], "address": addr, "reason": reason,
            "price_usd": pos["price"], "sol_usd": round(self.sol_usd or 0, 2),
            "sol_amount": round(proceeds, 6), "tokens_raw": pos["tokens_raw"],
            "pnl_sol": round(pnl, 6), "pnl_pct": round(pnl_pct, 2),
            "held_min": round(held, 1), "balance_sol": round(self.state["balance_sol"], 4),
        })

    def sell_all(self, reason):
        for addr in list(self.positions):
            self.sell(addr, reason)

    # ── watching ─────────────────────────────────────────────
    def refresh_prices(self):
        sol = sol_price_usd()
        if sol:
            self.sol_usd = sol
        if not self.positions:
            return

        # DexScreener: price + liquidity, used for the rug (liquidity pulled) check
        pairs = scanner.fetch_market_data(list(self.positions))
        for addr, pos in self.positions.items():
            p = pairs.get(addr)
            if p and p.get("priceUsd"):
                pos["price"] = float(p["priceUsd"])
                pos["liq"] = ((p.get("liquidity") or {}).get("usd")) or 0

        # Jupiter: what selling the whole position would actually pay right now
        for pos in self.positions.values():
            back, _ = jupiter_quote(pos["address"], SOL, pos["tokens_raw"])
            if not back:
                pos["misses"] += 1
                if pos["misses"] >= SETTINGS["missing_price_writeoff"]:
                    pos["value_sol"] = 0.0
                continue
            pos["misses"] = 0
            pos["value_sol"] = max(back / LAMPORTS - SETTINGS["network_fee_sol"], 0.0)
            pos["peak_value_sol"] = max(pos["peak_value_sol"], pos["value_sol"])

    def exit_reason(self, pos):
        s = SETTINGS
        pnl_pct = (pos["value_sol"] / pos["cost_sol"] - 1) * 100
        peak_pct = (pos["peak_value_sol"] / pos["cost_sol"] - 1) * 100
        from_peak = (1 - pos["value_sol"] / pos["peak_value_sol"]) * 100 if pos["peak_value_sol"] else 0
        held = (datetime.now() - pos["opened"]).total_seconds() / 60

        if pos["misses"] >= s["missing_price_writeoff"]:
            return "can't get a sell quote - written off"
        if pos["entry_liq"] and pos["liq"] < pos["entry_liq"] * (1 - s["rug_liquidity_drop_pct"] / 100):
            return "liquidity pulled"
        if pnl_pct <= -s["stop_loss_pct"]:
            return "stop loss"
        if pnl_pct >= s["take_profit_pct"]:
            return "take profit"
        if peak_pct >= s["trailing_arm_pct"] and from_peak >= s["trailing_stop_pct"]:
            return "trailing stop"
        if held >= s["max_hold_min"]:
            return "max hold time"
        return None

    def check_exits(self):
        for addr, pos in list(self.positions.items()):
            reason = self.exit_reason(pos)
            if reason:
                self.sell(addr, reason)

    def check_daily_limits(self):
        s, pnl = SETTINGS, self.day_pnl_sol()
        if pnl >= s["daily_profit_target_sol"]:
            why = f"daily profit target hit ({pnl:+.4f} SOL)"
        elif pnl <= -s["daily_loss_limit_sol"]:
            why = f"daily loss limit hit ({pnl:+.4f} SOL)"
        else:
            return False
        print(f"\n*** {why} - selling everything and stopping for today ***")
        self.sell_all("daily limit")
        self.state["day_stopped"] = True
        save_state(self.state)
        return True

    def status_line(self):
        parts = []
        for pos in self.positions.values():
            pnl_pct = (pos["value_sol"] / pos["cost_sol"] - 1) * 100
            flag = " (no quote)" if pos["misses"] else ""
            parts.append(f"{pos['symbol'][:8]} {pnl_pct:+.1f}%{flag}")
        print(f"[{datetime.now():%H:%M:%S}] balance {self.state['balance_sol']:.4f} SOL | "
              f"in play {self.in_play_sol():.3f} | today {self.day_pnl_sol():+.4f} SOL | "
              + (", ".join(parts) if parts else "no positions"))


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────
def run(end_time=None, reset=False):
    s = SETTINGS
    state = load_state(reset)
    save_state(state)
    if state["day_stopped"]:
        print(f"Already hit a daily limit today ({state['day_pnl_sol']:+.4f} SOL). "
              "Come back tomorrow, or delete paper_state.json.")
        return

    bot = PaperTrader(state)
    bot.sol_usd = sol_price_usd()
    if not bot.sol_usd:
        print("Couldn't get the SOL price - check your connection and try again.")
        return

    print(f"PAPER TRADING - balance {state['balance_sol']:.4f} SOL, SOL = ${bot.sol_usd:,.2f}")
    print(f"Caps: {s['sol_per_trade']} SOL/trade, {s['max_sol_in_play']} SOL in play, "
          f"daily +{s['daily_profit_target_sol']} / -{s['daily_loss_limit_sol']} SOL")
    print(f"Running until {end_time:%H:%M}" if end_time else "Running until Ctrl+C")

    next_scan = datetime.now()
    try:
        while True:
            if end_time and datetime.now() >= end_time:
                print("\nSession time is up - winding down.")
                break

            bot.refresh_prices()
            bot.check_exits()
            if bot.check_daily_limits():
                break

            if datetime.now() >= next_scan:
                candidates = scanner.scan(show_rejected=False)
                bot.pick_and_buy(candidates)
                next_scan = datetime.now() + timedelta(minutes=s["scan_every_min"])

            bot.status_line()
            time.sleep(s["check_prices_every_sec"])
    except KeyboardInterrupt:
        print("\nCtrl+C - winding down.")

    if bot.positions:
        print("Selling all open positions back to SOL...")
        bot.refresh_prices()
        bot.sell_all("wind down")

    start = s["starting_balance_sol"]
    print(f"\nDone. Paper balance {state['balance_sol']:.4f} SOL "
          f"({state['balance_sol'] - start:+.4f} vs start), today {state['day_pnl_sol']:+.4f} SOL")
    print(f"Trades logged to {TRADES_FILE}")


def parse_end_time(args):
    if args.hours:
        return datetime.now() + timedelta(hours=args.hours)
    if args.until:
        hh, mm = map(int, args.until.split(":"))
        end = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return end if end > datetime.now() else end + timedelta(days=1)
    return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Solana memecoin paper trader")
    ap.add_argument("--hours", type=float, help="stop and sell everything after N hours")
    ap.add_argument("--until", metavar="HH:MM", help="stop and sell everything at this time")
    ap.add_argument("--reset", action="store_true", help="start the paper wallet over")
    args = ap.parse_args()
    run(parse_end_time(args), args.reset)
