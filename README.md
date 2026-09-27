# solana-memecoin-bot

A Solana memecoin scanner and **paper-trading** bot. It finds tokens with real trading activity, filters out likely scams and rugs, and simulates trading them with fake SOL at real Jupiter quotes, so you can test a strategy before risking anything.

> **Paper mode only.** There is no wallet, no private key, and no real trading in this repo.
>
> **Not financial advice.** Most memecoins go to zero, and bots that trade them usually lose money to fees, slippage and faster bots. This is an experiment. Use at your own risk.

## What's in here

| File | What it does |
|---|---|
| `sol_scanner.py` | Finds trending Solana tokens (DexScreener), runs scam/rug checks (RugCheck), and sorts survivors into VOLATILE / HIGH_VOLUME / SLOW / OTHER buckets. Read-only. |
| `trader.py` | Paper trader. Scans every few minutes, buys candidates with fake SOL at real Jupiter quotes, and sells on take-profit, stop-loss, trailing stop, max hold time, or liquidity being pulled. |
| `analyze.py` | Report on your paper trades: win rate, profit factor, P&L by exit reason and bucket, stop-loss overshoot. |

## Setup

Python 3.9+.

```
python -m pip install -r requirements.txt
```

No API keys needed. It uses the free public DexScreener, RugCheck and Jupiter quote APIs.

## Usage

**Scan once** and see what passes:
```
python sol_scanner.py
python sol_scanner.py --show-rejected   # also list rejected coins and why
python sol_scanner.py --loop 5          # rescan every 5 minutes
```

**Paper trade:**
```
python trader.py                 # until Ctrl+C
python trader.py --hours 3       # for 3 hours
python trader.py --until 23:00   # until 23:00
python trader.py --reset         # start the paper wallet over
```
When the session ends (time up, Ctrl+C, or a daily limit is hit), every open position is sold back to SOL. Trades are logged to `paper_trades.csv`; balance and today's P&L persist in `paper_state.json`.

**Analyze results:**
```
python analyze.py
python analyze.py --since 2026-09-28
```
Get 50+ closed trades before trusting the numbers or changing settings.

## How it decides

**Scanner filters** (reject if any fail): minimum liquidity, 24h volume, age and trade count; near-zero sells (possible honeypot); RugCheck risk score; mint or freeze authority still enabled; too little liquidity locked; top 10 holders own too much; any RugCheck "danger" flag.

**Before each buy**, the trader asks Jupiter for a buy quote *and* a quote to sell straight back. If there's no sell route, or the round trip loses more than 5% (hidden taxes, thin pools), the coin is skipped.

**Entries:** VOLATILE or HIGH_VOLUME bucket, rising over the last 5 minutes, 1h change between -20% and +150%, not traded in the last hour.

**Exits** (measured on what Jupiter would actually pay, after costs): take profit +30%, stop loss -15%, trailing stop 12% from peak once +10% up, 90 minute max hold, or liquidity drops 50%.

**Risk caps:** 0.25 SOL per trade, 1 SOL in play, 4 positions max; stop for the day at +0.5 or -0.3 SOL.

All of these are in the `SETTINGS` dict at the top of each script.

## Known limits

- Prices are checked every 15 seconds, so fast crashes can sell well past the stop loss. `analyze.py` reports how far.
- Paper fills assume the Jupiter quote you see is the price you get. Live swaps can fill worse, and can fail.
- The free APIs rate-limit; the scripts back off and retry.

## License

MIT
