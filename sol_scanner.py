"""
Solana memecoin scanner + scam/rug checker  (Step 1 of the trading bot)

What it does:
  1. Finds Solana tokens that are getting attention right now (DexScreener).
  2. Pulls live market data for each one: price, volume, liquidity, buys/sells, age.
  3. Runs a scam/rug check on each (RugCheck.xyz): mint/freeze authority,
     locked liquidity, danger flags, holder concentration.
  4. Throws out anything that fails, and sorts the survivors into buckets:
        VOLATILE     - big fast price swings
        HIGH_VOLUME  - lots of trading relative to its liquidity
        SLOW         - steady, small moves
        OTHER        - passed the checks but doesn't fit a bucket
  5. Prints a report and saves it to scan_results.csv

It does NOT trade and needs NO wallet or API keys. It's read-only.

Setup:
    pip install requests
Run once:
    python sol_scanner.py
Run every 5 minutes (Ctrl+C to stop):
    python sol_scanner.py --loop 5
Show rejected coins and why:
    python sol_scanner.py --show-rejected
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone

import requests

# ─────────────────────────────────────────────────────────────
# SETTINGS - tweak these to make the scanner stricter or looser
# ─────────────────────────────────────────────────────────────
SETTINGS = {
    # Market filters
    "min_liquidity_usd": 20_000,     # thin pools = huge slippage + easy to rug
    "min_volume_24h_usd": 50_000,    # ignore dead coins
    "min_age_hours": 1.0,            # brand-new coins are the riskiest
    "min_txns_1h": 30,               # need real trading activity
    "min_sell_ratio_1h": 0.15,       # sells / buys; near-zero sells can mean you CAN'T sell (honeypot)

    # Scam / rug filters (RugCheck)
    "max_rugcheck_score": 60,        # RugCheck normalised score, higher = riskier (0-100)
    "min_lp_locked_pct": 80,         # % of liquidity locked or burned
    "max_top10_holder_pct": 35,      # top 10 wallets owning more than this = dump risk
    "reject_mint_authority": True,   # dev can print unlimited new tokens
    "reject_freeze_authority": True, # dev can freeze your tokens so you can't sell
    "reject_any_danger_flag": True,  # any RugCheck risk marked "danger"

    # Bucket rules
    "volatile_h1_pct": 15,           # |1h change| >= this  -> VOLATILE
    "volatile_h6_pct": 40,           # or |6h change| >= this
    "high_volume_turnover": 3.0,     # 24h volume / liquidity >= this -> HIGH_VOLUME
    "high_volume_min_24h": 250_000,
    "slow_h1_pct": 3,                # |1h| < this AND |6h| < slow_h6 -> SLOW
    "slow_h6_pct": 10,

    # Politeness toward the free APIs
    "rugcheck_delay_sec": 1.2,
    "max_tokens_to_check": 60,
}

DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

session = requests.Session()
session.headers["User-Agent"] = "sol-scanner/1.0"


def get_json(url, retries=3):
    """GET with simple retry and back-off on rate limits / network errors."""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"  ! request failed: {url[:80]}... ({e})", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


# ─────────────────────────────────────────────────────────────
# 1. DISCOVER candidate tokens
# ─────────────────────────────────────────────────────────────
def discover_tokens():
    """Collect Solana token addresses from DexScreener's 'latest' feeds."""
    feeds = [
        "/token-boosts/top/v1",
        "/token-boosts/latest/v1",
        "/token-profiles/latest/v1",
        "/community-takeovers/latest/v1",
    ]
    seen, out = set(), []
    for path in feeds:
        data = get_json(DEX + path) or []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            if item.get("chainId") != "solana":
                continue
            addr = item.get("tokenAddress")
            if addr and addr not in seen and addr != SOL_MINT and addr not in STABLES:
                seen.add(addr)
                out.append(addr)
    return out[: SETTINGS["max_tokens_to_check"]]


# ─────────────────────────────────────────────────────────────
# 2. MARKET DATA
# ─────────────────────────────────────────────────────────────
def fetch_market_data(addresses):
    """Return {token_address: best_pair} using the most liquid pair per token."""
    best = {}
    for i in range(0, len(addresses), 30):  # API allows 30 addresses per call
        chunk = ",".join(addresses[i : i + 30])
        pairs = get_json(f"{DEX}/tokens/v1/solana/{chunk}") or []
        for p in pairs:
            addr = (p.get("baseToken") or {}).get("address")
            if addr not in addresses:
                continue
            liq = ((p.get("liquidity") or {}).get("usd")) or 0
            if addr not in best or liq > ((best[addr].get("liquidity") or {}).get("usd") or 0):
                best[addr] = p
    return best


def summarize_pair(p):
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    txns_h1 = (p.get("txns") or {}).get("h1") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}
    created = p.get("pairCreatedAt")
    return {
        "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
        "name": (p.get("baseToken") or {}).get("name", "?"),
        "address": (p.get("baseToken") or {}).get("address"),
        "dex": p.get("dexId"),
        "quote": (p.get("quoteToken") or {}).get("symbol"),
        "price_usd": float(p.get("priceUsd") or 0),
        "liquidity_usd": ((p.get("liquidity") or {}).get("usd")) or 0,
        "market_cap": p.get("marketCap") or p.get("fdv") or 0,
        "vol_1h": vol.get("h1") or 0,
        "vol_24h": vol.get("h24") or 0,
        "chg_5m": chg.get("m5") or 0,
        "chg_1h": chg.get("h1") or 0,
        "chg_6h": chg.get("h6") or 0,
        "chg_24h": chg.get("h24") or 0,
        "buys_1h": txns_h1.get("buys") or 0,
        "sells_1h": txns_h1.get("sells") or 0,
        "age_hours": (now_ms - created) / 3_600_000 if created else None,
        "url": p.get("url"),
    }


def market_checks(t):
    """Return list of reasons to reject, based on market data alone."""
    s, bad = SETTINGS, []
    if t["liquidity_usd"] < s["min_liquidity_usd"]:
        bad.append(f"low liquidity ${t['liquidity_usd']:,.0f}")
    if t["vol_24h"] < s["min_volume_24h_usd"]:
        bad.append(f"low 24h volume ${t['vol_24h']:,.0f}")
    if t["age_hours"] is None or t["age_hours"] < s["min_age_hours"]:
        bad.append("too new")
    txns = t["buys_1h"] + t["sells_1h"]
    if txns < s["min_txns_1h"]:
        bad.append(f"only {txns} trades in 1h")
    if t["buys_1h"] > 0 and t["sells_1h"] / t["buys_1h"] < s["min_sell_ratio_1h"]:
        bad.append(f"almost no sells ({t['sells_1h']} vs {t['buys_1h']} buys) - honeypot?")
    return bad


# ─────────────────────────────────────────────────────────────
# 3. SCAM / RUG CHECK
# ─────────────────────────────────────────────────────────────
def rug_check(address):
    """Return (report_dict, list_of_reasons_to_reject)."""
    s, bad = SETTINGS, []
    rep = get_json(f"{RUG}/tokens/{address}/report")
    if not rep:
        return None, ["no RugCheck report available"]

    if rep.get("rugged"):
        bad.append("RugCheck marks it as RUGGED")

    score = rep.get("score_normalised")
    if score is not None and score > s["max_rugcheck_score"]:
        bad.append(f"RugCheck risk score {score}")

    if s["reject_mint_authority"] and rep.get("mintAuthority"):
        bad.append("mint authority still enabled")
    if s["reject_freeze_authority"] and rep.get("freezeAuthority"):
        bad.append("freeze authority still enabled")

    # Liquidity locked/burned: take the best-locked market
    lp_pcts = [
        ((m.get("lp") or {}).get("lpLockedPct"))
        for m in (rep.get("markets") or [])
        if (m.get("lp") or {}).get("lpLockedPct") is not None
    ]
    lp_locked = max(lp_pcts) if lp_pcts else None
    if lp_locked is not None and lp_locked < s["min_lp_locked_pct"]:
        bad.append(f"only {lp_locked:.0f}% liquidity locked")

    # Top-10 holder concentration, skipping pool/LP accounts where possible
    pool_accounts = set()
    for m in rep.get("markets") or []:
        for k in ("liquidityA", "liquidityB", "pubkey"):
            if m.get(k):
                pool_accounts.add(m[k])
        lp = m.get("lp") or {}
        for k in ("baseVault", "quoteVault"):
            if lp.get(k):
                pool_accounts.add(lp[k])
    holders = [
        h for h in (rep.get("topHolders") or [])
        if h.get("address") not in pool_accounts and h.get("owner") not in pool_accounts
    ]
    top10 = sum((h.get("pct") or 0) for h in holders[:10])
    if top10 > s["max_top10_holder_pct"]:
        bad.append(f"top 10 wallets hold {top10:.0f}%")

    dangers = [r.get("name") for r in (rep.get("risks") or []) if r.get("level") == "danger"]
    if s["reject_any_danger_flag"] and dangers:
        bad.append("danger: " + "; ".join(dangers))

    info = {
        "rug_score": score,
        "lp_locked_pct": lp_locked,
        "top10_pct": round(top10, 1),
        "holders": rep.get("totalHolders"),
        "warnings": "; ".join(
            r.get("name", "") for r in (rep.get("risks") or []) if r.get("level") == "warn"
        ),
    }
    return info, bad


# ─────────────────────────────────────────────────────────────
# 4. CLASSIFY
# ─────────────────────────────────────────────────────────────
def classify(t):
    s = SETTINGS
    h1, h6 = abs(t["chg_1h"]), abs(t["chg_6h"])
    turnover = t["vol_24h"] / t["liquidity_usd"] if t["liquidity_usd"] else 0
    t["turnover"] = round(turnover, 2)
    if h1 >= s["volatile_h1_pct"] or h6 >= s["volatile_h6_pct"]:
        return "VOLATILE"
    if turnover >= s["high_volume_turnover"] and t["vol_24h"] >= s["high_volume_min_24h"]:
        return "HIGH_VOLUME"
    if h1 < s["slow_h1_pct"] and h6 < s["slow_h6_pct"]:
        return "SLOW"
    return "OTHER"


# ─────────────────────────────────────────────────────────────
# 5. RUN + REPORT
# ─────────────────────────────────────────────────────────────
def scan(show_rejected=False, csv_path="scan_results.csv"):
    started = datetime.now()
    print(f"\n=== Scan started {started:%Y-%m-%d %H:%M:%S} ===")

    addrs = discover_tokens()
    print(f"Found {len(addrs)} Solana tokens to look at")
    if not addrs:
        return []

    pairs = fetch_market_data(addrs)
    passed, rejected = [], []

    for addr in addrs:
        p = pairs.get(addr)
        if not p:
            rejected.append(({"symbol": "?", "address": addr}, ["no market data"]))
            continue
        t = summarize_pair(p)

        reasons = market_checks(t)
        if reasons:  # skip the slower rug check if it already fails on market data
            rejected.append((t, reasons))
            continue

        info, rug_reasons = rug_check(addr)
        time.sleep(SETTINGS["rugcheck_delay_sec"])
        if rug_reasons:
            rejected.append((t, rug_reasons))
            continue

        t.update(info)
        t["bucket"] = classify(t)
        passed.append(t)

    order = {"VOLATILE": 0, "HIGH_VOLUME": 1, "SLOW": 2, "OTHER": 3}
    passed.sort(key=lambda x: (order[x["bucket"]], -x["vol_24h"]))

    print(f"\n{len(passed)} passed / {len(rejected)} rejected\n")
    for bucket in order:
        group = [t for t in passed if t["bucket"] == bucket]
        if not group:
            continue
        print(f"── {bucket} ({len(group)}) " + "─" * 40)
        for t in group:
            print(
                f"  {t['symbol'][:10]:<10} liq ${t['liquidity_usd']:>10,.0f}  "
                f"vol24 ${t['vol_24h']:>11,.0f}  1h {t['chg_1h']:>+6.1f}%  "
                f"6h {t['chg_6h']:>+6.1f}%  risk {t.get('rug_score')}  "
                f"top10 {t.get('top10_pct')}%"
            )
            if t.get("warnings"):
                print(f"             ⚠ {t['warnings'][:100]}")
        print()

    if show_rejected and rejected:
        print("── REJECTED " + "─" * 45)
        for t, why in rejected:
            print(f"  {t.get('symbol', '?')[:10]:<10} {'; '.join(why)[:110]}")
        print()

    if passed:
        cols = [
            "bucket", "symbol", "name", "address", "price_usd", "liquidity_usd",
            "market_cap", "vol_1h", "vol_24h", "turnover", "chg_5m", "chg_1h",
            "chg_6h", "chg_24h", "buys_1h", "sells_1h", "age_hours", "rug_score",
            "lp_locked_pct", "top10_pct", "holders", "warnings", "url",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(passed)
        print(f"Saved {len(passed)} coins to {csv_path}")

    print(f"Scan took {(datetime.now() - started).seconds}s")
    return passed


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Solana memecoin scanner + rug checker")
    ap.add_argument("--loop", type=float, metavar="MINUTES", help="repeat every N minutes")
    ap.add_argument("--show-rejected", action="store_true", help="list rejected coins and why")
    args = ap.parse_args()

    if args.loop:
        try:
            while True:
                scan(args.show_rejected)
                time.sleep(args.loop * 60)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        scan(args.show_rejected)
