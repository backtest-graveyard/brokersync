# BrokerSync for Ghostfolio

Daily, read-only sync of your brokerage holdings into [Ghostfolio](https://github.com/ghostfolio/ghostfolio) via [SnapTrade](https://snaptrade.com). No CSV export, nothing to re-key, nothing that breaks when a broker changes its export format.

Covers the brokers on SnapTrade's institution list (checked 2026-09-13): Trading 212, DEGIRO, Interactive Brokers, Schwab, Fidelity, Robinhood, Vanguard, E*TRADE, Webull, Public, tastytrade, eToro, Alpaca, Wealthsimple, Questrade, TD Direct, BMO InvestorLine, CIBC Investor's Edge, Coinbase, Kraken, Binance, AJ Bell, and more. **Not** covered: Trade Republic, XTB, Scalable Capital, Revolut, Saxo, Interactive Investor.

## What it does

For each connected brokerage account, mirror the current positions into a same-named Ghostfolio account as `BUY` activities (quantity = units, unit price = cost basis), and set the account's cash balance so the account total matches the broker. Ghostfolio prices everything live as usual.

Each run replaces that account's activities with the current snapshot, so re-runs never duplicate.

## What it does not do (yet)

- It does **not** import your historical trade list. Ghostfolio's time-weighted performance is right from the day you start syncing, not backfilled.
- It never places trades, moves money, or stores your broker login. The SnapTrade connection is requested read-only.

## The part that matters: it refuses to write stale numbers

A broker can revoke a connection and SnapTrade will keep returning the last cached balance with HTTP 200 for weeks. This sync reads SnapTrade's `sync_status.holdings.last_successful_sync` on every account and **skips any account whose data is older than `STALE_HOURS`** (default 36), logging it loudly instead of silently mirroring a dead number. Disabled connections are skipped the same way.

## Run it

1. Create a free SnapTrade developer account (Personal tier covers your own accounts), register one user, and connect your brokers through their portal.
2. In Ghostfolio, copy your Security Token (shown once at account creation).
3. `cp .env.example .env` and fill it in.
4. `python sync.py --dry-run` to see what it would do, then `python sync.py`.

Docker: `docker compose -f docker-compose.example.yml up -d` runs it daily.

## Status

A personal tool, shared as-is. I run it daily against my own accounts. Issues and pull requests are welcome; there is no paid version and no support promise.

## Not affiliated

Not affiliated with Ghostfolio, SnapTrade, or any brokerage. Ghostfolio is AGPL-3.0 and is not modified by this tool; BrokerSync talks to it over the public REST API and is MIT licensed.
