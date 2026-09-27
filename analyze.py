"""
Paper trading report  (reads paper_trades.csv from trader.py)

What it shows:
  - Overall: closed trades, win rate, total P&L, average win vs average loss,
    profit factor, expected P&L per trade
  - P&L broken down by exit reason (stop loss, take profit, trailing stop, ...)
  - P&L broken down by scanner bucket the coin was bought from
  - Stop-loss overshoot: how far past your stop the sells actually landed
  - P&L per day
  - Best and worst trades

Run:
    python analyze.py
    python analyze.py --since 2026-09-27
    python analyze.py --file some_other_trades.csv
"""

import argparse
import csv
import os
from collections import defaultdict

from trader import SETTINGS, TRADES_FILE


def load_trades(path, since=None):
    """Match each SELL to its BUY (same token, first in first out)."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    open_buys = defaultdict(list)
    closed = []
    for r in rows:
        if since and r["time"][:10] < since:
            continue
        if r["side"] == "BUY":
            open_buys[r["address"]].append(r)
        elif r["side"] == "SELL":
            buy = open_buys[r["address"]].pop(0) if open_buys[r["address"]] else None
            closed.append({
                "symbol": r["symbol"],
                "day": r["time"][:10],
                "bought": buy["time"] if buy else "?",
                "bucket": buy["reason"] if buy else "?",
                "exit": r["reason"],
                "cost_sol": float(buy["sol_amount"]) if buy else float("nan"),
                "pnl_sol": float(r["pnl_sol"]),
                "pnl_pct": float(r["pnl_pct"]),
                "held_min": float(r["held_min"]),
            })
    still_open = [b for buys in open_buys.values() for b in buys]
    return closed, still_open


def stats(trades):
    wins = [t for t in trades if t["pnl_sol"] > 0]
    losses = [t for t in trades if t["pnl_sol"] <= 0]
    gross_win = sum(t["pnl_sol"] for t in wins)
    gross_loss = -sum(t["pnl_sol"] for t in losses)
    n = len(trades)
    return {
        "n": n,
        "win_rate": len(wins) / n * 100 if n else 0,
        "pnl": sum(t["pnl_sol"] for t in trades),
        "avg_pct": sum(t["pnl_pct"] for t in trades) / n if n else 0,
        "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0,
        "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0,
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
        "avg_hold": sum(t["held_min"] for t in trades) / n if n else 0,
    }


def breakdown(title, trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[t[key]].append(t)
    print(f"\n── {title} " + "─" * (50 - len(title)))
    print(f"  {'':<24}{'trades':>7}{'win %':>8}{'P&L SOL':>11}{'avg %':>8}{'avg hold':>10}")
    for name, group in sorted(groups.items(), key=lambda kv: stats(kv[1])["pnl"]):
        s = stats(group)
        print(f"  {name[:24]:<24}{s['n']:>7}{s['win_rate']:>7.0f}%{s['pnl']:>+11.4f}"
              f"{s['avg_pct']:>+7.1f}%{s['avg_hold']:>8.0f}m")


def report(closed, still_open):
    if not closed:
        print("No closed trades yet - run trader.py for a while first.")
        if still_open:
            print(f"({len(still_open)} buys still open in the log)")
        return

    s = stats(closed)
    days = sorted({t["day"] for t in closed})
    print(f"=== Paper trading report: {days[0]} to {days[-1]} ===\n")
    print(f"  Closed trades     {s['n']}")
    print(f"  Win rate          {s['win_rate']:.0f}%")
    print(f"  Total P&L         {s['pnl']:+.4f} SOL")
    print(f"  Per trade         {s['pnl'] / s['n']:+.4f} SOL  ({s['avg_pct']:+.1f}% avg)")
    print(f"  Average win       {s['avg_win_pct']:+.1f}%")
    print(f"  Average loss      {s['avg_loss_pct']:+.1f}%")
    pf = s["profit_factor"]
    print(f"  Profit factor     {'n/a (no losses)' if pf == float('inf') else f'{pf:.2f}'}"
          "   (money won / money lost; above 1.0 = profitable)")
    print(f"  Average hold      {s['avg_hold']:.0f} min")
    if s["n"] < 50:
        print(f"\n  ⚠ Only {s['n']} trades - too few to trust. Aim for 50+ before changing settings.")

    breakdown("By exit reason", closed, "exit")
    breakdown("By scanner bucket", closed, "bucket")
    breakdown("By day", closed, "day")

    stops = [t for t in closed if t["exit"] == "stop loss"]
    if stops:
        target = -SETTINGS["stop_loss_pct"]
        avg = sum(t["pnl_pct"] for t in stops) / len(stops)
        worst = min(t["pnl_pct"] for t in stops)
        print(f"\n── Stop-loss overshoot " + "─" * 28)
        print(f"  Stop set at {target:.0f}%, stops actually averaged {avg:+.1f}% "
              f"(worst {worst:+.1f}%)")
        print(f"  Overshoot: {avg - target:+.1f} points on average")

    ranked = sorted(closed, key=lambda t: t["pnl_pct"])
    print(f"\n── Best and worst trades " + "─" * 26)
    for label, group in (("best", ranked[::-1][:3]), ("worst", ranked[:3])):
        for t in group:
            print(f"  {label:<6}{t['symbol'][:10]:<11}{t['pnl_pct']:>+7.1f}%  {t['pnl_sol']:>+8.4f} SOL"
                  f"  held {t['held_min']:>4.0f}m  [{t['exit']}]  {t['bought'][:16]}")

    if still_open:
        print(f"\n({len(still_open)} buys still open in the log - not counted)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Paper trading report")
    ap.add_argument("--file", default=TRADES_FILE, help="trade log to read")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="only trades on or after this date")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"No trade log at {args.file} - run trader.py first.")
    else:
        report(*load_trades(args.file, args.since))
