#!/usr/bin/env python3
"""
chipotle_burrito_nowcast.py

Nowcast the national-average Chipotle *chicken burrito* price by pulling EVERY
US Chipotle location's live online menu and averaging the burrito price.

This is the measured panel behind the KXCHIPBURRITO Kalshi market, which settles
on Spice Data's national-average "Chicken Burrito" price for the month. It reads
the same public menu prices Chipotle shows every customer in the pickup order
flow -- no login, no private data. Be polite (this script self-throttles).

Endpoints (Chipotle's own Azure API Management gateway, public client key):
  POST https://services.chipotle.com/restaurant/v3/restaurant           (store search)
  GET  https://services.chipotle.com/menuinnovation/v1/restaurants/{id}/onlinemenu?channelId=web

Enumeration: recursive quadtree over CONUS + AK + HI. Each box is queried once;
if it saturates (returns >= CAP stores) it splits into 4 and recurses, so dense
metros get refined automatically and completeness doesn't depend on a per-query
result cap. All returned stores are deduped globally by restaurant number.

Usage:
  python chipotle_burrito_nowcast.py                 # full run, all stores
  python chipotle_burrito_nowcast.py --sample 150    # quick test (nearest ~N to US center)
  python chipotle_burrito_nowcast.py --dump-first    # dump one raw menu JSON, then continue
  python chipotle_burrito_nowcast.py --workers 6     # menu-fetch concurrency (default 8)

Outputs (next to this script):
  chipotle_stores.json                 cached store list (id -> meta)
  chipotle_burrito_prices_YYYYMMDD.csv per-store prices for this run
  prints a summary: N priced, mean/median/trimmed mean, distribution, and the
  KXCHIPBURRITO strike comparison.
"""
import argparse, csv, json, math, os, random, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

KEY      = "b4d9f36380184a3788857063bce25d6a"   # public subscription key from Chipotle's web bundle
SEARCH   = "https://services.chipotle.com/restaurant/v3/restaurant"
MENU     = "https://services.chipotle.com/menuinnovation/v1/restaurants/{id}/onlinemenu?channelId=web"
HDRS     = {"Content-Type": "application/json", "Ocp-Apim-Subscription-Key": KEY,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
HERE     = os.path.dirname(os.path.abspath(__file__))

US_STATES = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS",
             "KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
             "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV",
             "WI","WY","DC"}  # Spice tracks the U.S. average -> exclude CA provinces (AB/BC/ON, priced in CAD)

CAP      = 200      # if a search box returns >= this, treat as saturated and subdivide
PAGESIZE = 250
MIN_DEG  = 0.12     # smallest quadtree box edge (~13 km) -- recursion floor
THROTTLE = 0.03     # seconds between HTTP calls per worker (politeness)

# ---------- HTTP ----------
def _req(url, data=None, tries=4):
    for i in range(tries):
        try:
            r = urllib.request.Request(url, data=data, method=("POST" if data else "GET"), headers=HDRS)
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):        # store has no online menu / bad id -> give up quietly
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep((2 ** i) * 0.5 + random.random() * 0.3)
    return None

def search(lat, lon, radius_m, page=0):
    body = {"latitude": lat, "longitude": lon, "radius": int(radius_m),
            "restaurantStatuses": ["OPEN", "LAB"], "conceptIds": ["CMG"],
            "orderBy": "distance", "pageSize": PAGESIZE, "pageIndex": page,
            "embeds": {"addressTypes": ["MAIN"], "realHours": False}}
    time.sleep(THROTTLE)
    return _req(SEARCH, data=json.dumps(body).encode())

# ---------- response helpers (defensive about field names) ----------
def _rlist(js):
    if not js: return []
    for k in ("data", "restaurants", "results", "items"):
        v = js.get(k)
        if isinstance(v, list): return v
    return []

def _rid(r):
    for k in ("restaurantNumber", "id", "restaurantId", "number"):
        if r.get(k) not in (None, ""): return str(r[k])
    return None

def _rlatlon(r):
    for latk, lonk in (("latitude", "longitude"), ("lat", "lng"), ("lat", "lon")):
        if r.get(latk) is not None and r.get(lonk) is not None:
            return float(r[latk]), float(r[lonk])
    for ak in ("addresses", "address"):
        a = r.get(ak)
        if isinstance(a, list) and a: a = a[0]
        if isinstance(a, dict):
            for latk, lonk in (("latitude", "longitude"), ("lat", "lng")):
                if a.get(latk) is not None and a.get(lonk) is not None:
                    return float(a[latk]), float(a[lonk])
    return None, None

def _rstate(r):
    for ak in ("addresses", "address"):
        a = r.get(ak)
        if isinstance(a, list) and a: a = a[0]
        if isinstance(a, dict):
            return a.get("administrativeArea") or a.get("state") or a.get("region") or ""
    return r.get("state", "")

def haversine_km(a, b):
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2 * R * math.asin(math.sqrt(h))

# ---------- enumeration ----------
def enumerate_stores(sample=None):
    stores = {}
    if sample:
        js = search(39.8283, -98.5795, 5_000_000, page=0)
        for r in _rlist(js)[:sample]:
            rid = _rid(r)
            if rid:
                lat, lon = _rlatlon(r)
                stores[rid] = {"id": rid, "name": r.get("restaurantName") or r.get("name") or "",
                               "lat": lat, "lon": lon, "state": _rstate(r)}
        return stores

    # quadtree boxes: CONUS, Alaska, Hawaii
    boxes = [(24.4, 49.5, -125.0, -66.8), (51.0, 71.5, -170.0, -129.0), (18.9, 22.3, -160.3, -154.7)]
    q = list(boxes)
    n_queries = 0
    while q:
        la0, la1, lo0, lo1 = q.pop()
        clat, clon = (la0+la1)/2, (lo0+lo1)/2
        radius_km = haversine_km((clat, clon), (la1, lo1)) + 2
        js = search(clat, clon, radius_km * 1000, page=0)
        n_queries += 1
        rl = _rlist(js)
        for r in rl:
            rid = _rid(r)
            if rid and rid not in stores:
                lat, lon = _rlatlon(r)
                stores[rid] = {"id": rid, "name": r.get("restaurantName") or r.get("name") or "",
                               "lat": lat, "lon": lon, "state": _rstate(r)}
        saturated = len(rl) >= CAP
        splittable = (la1 - la0) > MIN_DEG and (lo1 - lo0) > MIN_DEG
        if saturated and splittable:
            mlat, mlon = (la0+la1)/2, (lo0+lo1)/2
            q += [(la0, mlat, lo0, mlon), (la0, mlat, mlon, lo1),
                  (mlat, la1, lo0, mlon), (mlat, la1, mlon, lo1)]
        if n_queries % 25 == 0:
            print(f"  ...{n_queries} search queries, {len(stores)} unique stores so far", flush=True)
    print(f"  enumeration done: {n_queries} queries, {len(stores)} unique stores", flush=True)
    return stores

# ---------- menu price extraction ----------
def _walk(node, out):
    """Collect every dict that looks like a menu item (has a name + a price)."""
    if isinstance(node, dict):
        name = node.get("itemName") or node.get("name") or node.get("displayName") or ""
        price = node.get("unitPrice", node.get("price"))
        if name and isinstance(price, (int, float)):
            out.append((str(name), float(price), node))
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)

def chicken_burrito_price(menu):
    """
    Return (price, method) for the plain Chicken Burrito, or (None, reason).
    Chipotle lists each protein as its own burrito item carrying a primaryFillingName.
    Match primaryFillingName == 'Chicken' exactly on a Burrito (not Bowl) item, so
    'Chipotle Honey Chicken', 'Steak', 'Barbacoa', etc. are all excluded.
    """
    items = []
    _walk(menu, items)
    if not items:
        return None, "no-priced-items"
    cands = []  # (rank, name, price); lower rank preferred
    for (name, price, node) in items:
        nl = name.lower()
        if "burrito" not in nl or "bowl" in nl:
            continue
        filling = str(node.get("primaryFillingName", "")).strip().lower()
        if filling == "chicken":
            cands.append((0, name, price))
        elif nl == "chicken burrito":
            cands.append((1, name, price))
    if not cands:
        return None, "no-chicken-burrito"
    cands.sort(key=lambda x: x[0])
    _, name, price = cands[0]
    if 6.0 <= price <= 16.0:
        return round(price, 2), f"burrito:{name}"
    return round(price, 2), f"burrito-oor:{name}"

def fetch_price(store):
    menu = _req(MENU.format(id=store["id"]))
    time.sleep(THROTTLE)
    if not menu:
        return store["id"], None, "no-menu", store
    price, method = chicken_burrito_price(menu)
    return store["id"], price, method, store

# ---------- stats ----------
def summarize(rows):
    def us_valid(p, s):
        return p is not None and 6.0 <= p <= 16.0 and (s.get("state") or "").strip().upper() in US_STATES
    vals = sorted(p for (_, p, _, s) in rows if us_valid(p, s))
    dropped = sum(1 for (_, p, _, s) in rows if p is not None and not us_valid(p, s))
    n = len(vals)
    if n == 0:
        print("No prices extracted."); return
    print(f"(excluded {dropped} non-US / out-of-band reads from the average)")
    mean = sum(vals) / n
    med = vals[n//2] if n % 2 else (vals[n//2-1] + vals[n//2]) / 2
    k = int(n * 0.10)
    trimmed = sum(vals[k:n-k]) / (n - 2*k) if n - 2*k > 0 else mean
    var = sum((v-mean)**2 for v in vals) / (n-1) if n > 1 else 0.0
    std = var ** 0.5
    sem = std / (n ** 0.5)
    print("\n" + "=" * 64)
    print(f"CHIPOTLE CHICKEN BURRITO -- national panel  ({datetime.now(timezone.utc):%Y-%m-%d %H:%MZ})")
    print("=" * 64)
    print(f"stores priced      : {n}")
    print(f"national MEAN      : ${mean:.4f}   (this is the settlement-relevant number)")
    print(f"  +/- SEM          : ${sem:.4f}   -> 95% CI ${mean-1.96*sem:.3f} - ${mean+1.96*sem:.3f}")
    print(f"median             : ${med:.2f}")
    print(f"10% trimmed mean   : ${trimmed:.4f}")
    print(f"std dev / min / max: ${std:.3f} / ${vals[0]:.2f} / ${vals[-1]:.2f}")

    # modal price points
    from collections import Counter
    c = Counter(round(v, 2) for v in vals)
    print("\ntop price points   : " + ", ".join(f"${p:.2f}x{n_}" for p, n_ in c.most_common(8)))

    # KXCHIPBURRITO comparison. Settlement is on the MEAN vs strike.
    print("\nKXCHIPBURRITO August ladder (settles on Spice national avg):")
    print("  prior prints: Jun $9.75, Jul $9.77")
    print(f"  {'strike':>7}  {'mean>=strike?':>13}  {'% stores >= strike':>18}")
    for k_ in [9.77, 9.78, 9.79, 9.80, 9.81, 9.82]:
        share = 100.0 * sum(1 for v in vals if v >= k_) / n
        hit = "YES" if mean >= k_ else "no"
        print(f"  ${k_:>5.2f}  {hit:>13}  {share:>17.1f}%")
    print("\nNote: 'mean>=strike' is the panel's point nowcast; residual settlement")
    print("uncertainty is (a) drift to month-end and (b) any level offset between this")
    print("pickup-price panel and Spice's basket -- calibrate vs the $9.77 Jul print.")

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None, help="only price nearest ~N to US center (quick test)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dump-first", action="store_true", help="dump one raw menu JSON for inspection")
    ap.add_argument("--refresh-stores", action="store_true", help="ignore cached store list")
    args = ap.parse_args()

    cache = os.path.join(HERE, "chipotle_stores.json")
    if args.sample:
        print(f"[1/2] enumerating (sample={args.sample})...", flush=True)
        stores = enumerate_stores(sample=args.sample)
    elif os.path.exists(cache) and not args.refresh_stores:
        stores = json.load(open(cache))
        print(f"[1/2] loaded {len(stores)} stores from cache ({cache})", flush=True)
    else:
        print("[1/2] enumerating ALL US Chipotle stores via quadtree...", flush=True)
        stores = enumerate_stores()
        json.dump(stores, open(cache, "w"))
        print(f"      cached -> {cache}", flush=True)

    store_list = list(stores.values())
    if not store_list:
        print("No stores found -- the API shape may have changed. Re-run with --dump-first.")
        sys.exit(1)

    if args.dump_first:
        m = _req(MENU.format(id=store_list[0]["id"]))
        p = os.path.join(HERE, "chipotle_menu_sample.json")
        json.dump(m, open(p, "w"), indent=2)
        print(f"      dumped sample menu -> {p}  (price={chicken_burrito_price(m)})", flush=True)

    print(f"[2/2] fetching menus for {len(store_list)} stores @ {args.workers} workers...", flush=True)
    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch_price, s) for s in store_list]
        for f in as_completed(futs):
            rows.append(f.result())
            done += 1
            if done % 200 == 0:
                got = sum(1 for _, p, _, _ in rows if p is not None)
                print(f"  ...{done}/{len(store_list)} fetched, {got} priced", flush=True)

    stamp = datetime.now().strftime("%Y%m%d")
    out = os.path.join(HERE, f"chipotle_burrito_prices_{stamp}.csv")
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["restaurant_id", "name", "state", "lat", "lon", "chicken_burrito_price", "method"])
        for rid, price, method, s in sorted(rows, key=lambda x: (x[3].get("state") or "", x[0])):
            w.writerow([rid, s.get("name"), s.get("state"), s.get("lat"), s.get("lon"), price, method])
    print(f"      per-store CSV -> {out}", flush=True)

    summarize(rows)

if __name__ == "__main__":
    main()
