#!/usr/bin/env python3
"""BrokerSync: SnapTrade -> Ghostfolio daily holdings sync (read-only).

For every brokerage account connected to your SnapTrade user, mirror the current
positions into a same-named Ghostfolio account as BUY activities
(quantity = units, unitPrice = cost basis, so unrealized gain is right).
Ghostfolio prices the holdings live as usual.

Snapshot model: each run deletes that Ghostfolio account's existing activities and
re-imports the current positions. Re-runs never duplicate. It does NOT import your
historical trade list; see README.

Usage:
  python sync.py                 # sync every connected account
  python sync.py <snaptrade_account_id>   # one account (testing)
  python sync.py --dry-run       # fetch and plan, write nothing to Ghostfolio

Config is entirely from environment variables; see .env.example.
"""
import os
import re
import sys
import time
import logging
import requests
from snaptrade_client import SnapTrade

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("brokersync")

VERSION = "0.1.0"

CLIENT_ID = os.environ["SNAPTRADE_CLIENT_ID"]
CONSUMER_KEY = os.environ["SNAPTRADE_CONSUMER_KEY"]
USER_ID = os.environ["SNAPTRADE_USER_ID"]
USER_SECRET = os.environ["SNAPTRADE_USER_SECRET"]
GF_URL = os.environ.get("GHOSTFOLIO_URL", "http://ghostfolio:3333").rstrip("/")
GF_TOKEN = os.environ["GHOSTFOLIO_TOKEN"]
LOT_DATE = os.environ.get("SYNC_LOT_DATE", "2024-01-01")   # date stamped on the synthetic BUYs
STALE_HOURS = float(os.environ.get("STALE_HOURS", "36"))   # warn if SnapTrade's last sync is older

snap = SnapTrade(consumer_key=CONSUMER_KEY, client_id=CLIENT_ID)
DISABLED = []   # authorization ids SnapTrade reports as disabled this run


def body(r):
    return getattr(r, "body", r)


# ---------------- SnapTrade ----------------
def list_accounts():
    """All connected brokerage accounts across every authorization."""
    auths = body(snap.connections.list_brokerage_authorizations(
        user_id=USER_ID, user_secret=USER_SECRET))
    out = []
    for au in auths:
        if au.get("disabled"):
            DISABLED.append(au.get("id"))
            log.info(f"! connection {au.get('brokerage', {}).get('name', au['id'])} is DISABLED "
                     f"(since {au.get('disabled_date')}). Reconnect it in SnapTrade; skipping.")
            continue
        out.extend(body(snap.connections.list_brokerage_authorization_accounts(
            authorization_id=au["id"], user_id=USER_ID, user_secret=USER_SECRET)))
    return out


def freshness(acct):
    """SnapTrade's own sync_status: the only honest signal of how old the data is.
    A 200 response is NOT evidence of fresh data; a broker can revoke a grant and
    SnapTrade will keep serving the last cached balance for weeks."""
    ss = (acct.get("sync_status") or {}).get("holdings") or {}
    ts = ss.get("last_successful_sync")
    if not ts:
        return None, "no sync_status"
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
        return age_h, f"last_successful_sync {t.strftime('%Y-%m-%d %H:%M')} UTC ({age_h:.0f}h ago)"
    except Exception:
        return None, f"last_successful_sync {ts}"


# ---------------- Ghostfolio ----------------
class Ghostfolio:
    def __init__(self):
        r = requests.post(f"{GF_URL}/api/v1/auth/anonymous", json={"accessToken": GF_TOKEN}, timeout=20)
        r.raise_for_status()
        self.h = {"Authorization": f"Bearer {r.json()['authToken']}"}

    def accounts(self):
        r = requests.get(f"{GF_URL}/api/v1/account", headers=self.h, timeout=20)
        r.raise_for_status()
        return r.json().get("accounts", [])

    def create_account(self, name, currency="USD"):
        r = requests.post(f"{GF_URL}/api/v1/account", headers=self.h,
                          json={"balance": 0, "comment": None, "currency": currency,
                                "isExcluded": False, "name": name, "platformId": None},
                          timeout=20)
        r.raise_for_status()
        return r.json()

    def orders(self, account_id):
        r = requests.get(f"{GF_URL}/api/v1/activities", headers=self.h, timeout=30)
        r.raise_for_status()
        out = []
        for o in r.json().get("activities", []):
            aid = o.get("accountId") or (o.get("account") or {}).get("id")
            if aid == account_id:
                out.append(o)
        return out

    def delete_order(self, oid):
        r = requests.delete(f"{GF_URL}/api/v1/activities/{oid}", headers=self.h, timeout=20)
        r.raise_for_status()  # never fail silently; silent deletes cause duplicates

    def set_account_balance(self, account_id, name, balance, currency="USD", excluded=False):
        r = requests.put(f"{GF_URL}/api/v1/account/{account_id}", headers=self.h,
                         json={"id": account_id, "balance": round(balance, 2), "comment": None,
                               "currency": currency, "isExcluded": excluded, "name": name,
                               "platformId": None}, timeout=20)
        r.raise_for_status()

    def lookup(self, query):
        try:
            r = requests.get(f"{GF_URL}/api/v1/symbol/lookup", headers=self.h,
                             params={"query": query}, timeout=20)
            return r.json().get("items", []) if r.status_code == 200 else []
        except Exception:
            return []

    def import_activities(self, activities):
        return requests.post(f"{GF_URL}/api/v1/import", headers=self.h,
                             json={"activities": activities}, timeout=90)

    def import_with_retry(self, activities, tries=5, delay=12, spacing=3):
        """Rate-limited data sources (CoinGecko): one at a time with backoff."""
        imported, skipped = 0, []
        for a in activities:
            ok = False
            for _ in range(tries):
                r = self.import_activities([a])
                if r.status_code in (200, 201):
                    ok = True
                    break
                time.sleep(delay)
            if ok:
                imported += 1
            else:
                skipped.append(a["symbol"])
            time.sleep(spacing)
        return imported, skipped

    def import_resilient(self, activities):
        """Ghostfolio's import is atomic and rejects on the first unknown symbol.
        Drop the offender and retry so the rest still import."""
        acts = list(activities)
        skipped = []
        while acts:
            r = self.import_activities(acts)
            if r.status_code in (200, 201):
                return len(acts), skipped
            try:
                msg = r.json().get("message")
                if isinstance(msg, list):
                    msg = " ".join(str(x) for x in msg)
            except Exception:
                msg = r.text
            m = re.search(r'activities\.(\d+)\.symbol \("?([^"\)]+)"?\)', msg or "")
            if not m:
                raise RuntimeError(f"import error: {(msg or '')[:300]}")
            idx, bad = int(m.group(1)), m.group(2)
            skipped.append(bad)
            del acts[idx]
        return 0, skipped


def resolve_symbol(gf, pos):
    """(dataSource, symbol) for Ghostfolio. Equities via YAHOO by ticker
    (BRK.B -> BRK-B). Crypto via Ghostfolio's lookup (CoinGecko). Unmatched
    symbols are folded into the account's cash so nothing is lost."""
    inst = pos["instrument"]
    ticker = (inst.get("symbol") or "").strip()
    name = (inst.get("description") or "").strip()
    if inst.get("kind") != "crypto":
        return ("YAHOO", ticker.replace(".", "-"))
    seen, cands = set(), []
    for q in (name, ticker):
        if not q:
            continue
        for i in gf.lookup(q):
            if i.get("assetSubClass") != "CRYPTOCURRENCY":
                continue
            k = (i.get("dataSource"), i.get("symbol"))
            if k not in seen:
                seen.add(k)
                cands.append(i)
    nl = name.lower()
    for i in cands:
        if (i.get("name") or "").lower() == nl:
            return (i["dataSource"], i["symbol"])
    if len(nl) >= 3:
        for i in cands:
            inm = (i.get("name") or "").lower()
            if inm and (inm in nl or nl in inm):
                return (i["dataSource"], i["symbol"])
    return None


def to_activity(pos, account_id, ds, sym):
    return {
        "accountId": account_id,
        "currency": pos.get("currency", "USD"),
        "dataSource": ds,
        "date": f"{LOT_DATE}T00:00:00.000Z",
        "fee": 0,
        "quantity": float(pos["units"]),
        "symbol": sym,
        "type": "BUY",
        "unitPrice": float(pos.get("cost_basis") or pos.get("price") or 0),
    }


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    positional = [a for a in args if not a.startswith("--")]
    only = positional[0] if positional else None
    log.info(f"brokersync {VERSION}  ·  read-only  ·  {'DRY RUN' if dry else 'live'}")

    gf, gf_by_name = None, {}
    if not dry:
        gf = Ghostfolio()
        gf_by_name = {a["name"]: a for a in gf.accounts()}
        log.info(f"Ghostfolio: {GF_URL}  ·  auth ok  ·  {len(gf_by_name)} existing accounts")

    t0 = time.time()
    sa = list_accounts()
    log.info(f"SnapTrade: {len(sa)} accounts")
    total, stale, fresh = 0, 0, 0
    claimed = set()   # Ghostfolio account names already written this run
    for acct in sa:
        aid, name = acct["id"], acct["name"]
        if only and aid != only:
            continue
        age_h, note = freshness(acct)
        if age_h is not None and age_h > STALE_HOURS:
            stale += 1
            log.info(f"! {name}: STALE  ·  {note}  ·  data older than {STALE_HOURS:.0f}h, "
                     "the broker connection probably needs a reconnect. Skipping so a dead number is never written.")
            continue
        fresh += 1
        pos = body(snap.account_information.get_all_account_positions(
            user_id=USER_ID, user_secret=USER_SECRET, account_id=aid)).get("results", [])
        snap_total = ((acct.get("balance") or {}).get("total") or {}).get("amount")
        log.info(f"- {name}: {len(pos)} positions fetched  ·  {note}")
        if not pos and (not snap_total or abs(float(snap_total)) < 0.01):
            log.info(f"- {name}: empty, skipping")
            continue
        # Brokers reuse names ("Individual", "Robinhood Individual"). Two accounts
        # mapped to one Ghostfolio account would each wipe the other's activities,
        # because every run replaces an account's snapshot. Disambiguate the
        # second and later ones with a stable suffix from the SnapTrade account id.
        if name in claimed:
            name = f"{name} ({aid[:8]})"
            log.info(f"  name collision: using Ghostfolio account '{name}'")
        claimed.add(name)
        if dry:
            continue

        gfa = gf_by_name.get(name) or gf.create_account(name)
        gid = gfa["id"]
        gf_by_name[name] = gfa

        acts, mkt_by_sym = [], {}
        for p in pos:
            res = resolve_symbol(gf, p)
            if not res:
                continue
            ds, sym = res
            acts.append(to_activity(p, gid, ds, sym))
            mkt_by_sym[sym] = mkt_by_sym.get(sym, 0.0) + float(p["units"]) * float(p.get("price") or 0)

        for o in gf.orders(gid):
            gf.delete_order(o["id"])
        yahoo = [a for a in acts if a["dataSource"] == "YAHOO"]
        other = [a for a in acts if a["dataSource"] != "YAHOO"]
        imp_y, skip_y = gf.import_resilient(yahoo)
        imp_o, skip_o = gf.import_with_retry(other)
        imported = imp_y + imp_o
        skipped = skip_y + skip_o
        total += imported
        imported_mkt = sum(v for s, v in mkt_by_sym.items() if s not in skipped)

        cash_note = ""
        if snap_total is not None:
            cash = float(snap_total) - imported_mkt   # real cash + anything unmatched
            gf.set_account_balance(gid, name, cash, excluded=(len(pos) == 0))
            cash_note = f"  cash/other=${cash:,.0f}"
        skip_note = f"  skipped={','.join(skipped)}" if skipped else ""
        log.info(f"+ {name}: {imported} positions{cash_note}{skip_note}")

    log.info(f"verify: {fresh}/{fresh + stale} accounts fresh (<{STALE_HOURS:.0f}h)  ·  "
             f"{stale} stale skipped  ·  {len(DISABLED)} connections disabled")
    log.info(f"DONE total_positions_imported={total}  ·  {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
