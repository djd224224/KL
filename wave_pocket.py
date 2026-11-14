"""Wave Pocket: the wave dashboards (imm_pvd_watch, ports 8771-8779) on the phone,
anywhere, as a private claude.ai page. Jack 2026-11-07: "Dashboards need to be
accessible on mobile away from home WiFi; like the IMM pocket refresh".

Like IMM Pocket it refreshes ON REQUEST ("refresh wave pocket", user skill
wave-pocket): nothing here runs on a schedule.

  python wave_pocket.py --summary
      Reads /data from every running wave dashboard (loopback, no key needed),
      writes run-logs/pvd-scan/wave_pocket.json (the page's pocket/latest
      document: each dashboard's /data, gzip + base64), prints one line a
      dashboard. Then ArtifactData `set` pocket/latest from that file.
  python wave_pocket.py --page OUT.html
      Builds the page itself from the live dashboard page (same tables and
      charts), with the current build embedded. Only a page change needs this
      (then publish OUT.html to the artifact's url).

The document must stay under the store's 256 KiB cap: a dashboard that would
push it over has its charts thinned, then its markets table cut to the
biggest markets (the page says so).
"""
import argparse
import base64
import gzip
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "run-logs", "pvd-scan", "wave_pocket.json")
REG = os.path.join(HERE, "run-logs", "pvd-scan", "waves.json")
URL = "https://claude.ai/artifact/7cLPPWUN2ui5FhZPcj67Y6"
CAP = 240_000            # bytes of JSON; the store refuses a document over 256 KiB

# the fixed dashboards (pvd_watch_keepalive.ps1); registry waves are added from waves.json
FIXED = [
    # 8773 / 8771 retired 2026-11-14 (their markets are registry waves now)
    ("e1104", 8774, "11/04 election wave"),
]


# TAB NAMES (Jack 2026-11-11: "Name them clearly in wave pocket. I can't tell which are
# the 2 waves from today"). Each tab says when and what in plain words; the line under
# it says what it covers and what it overlaps. Order: the registry's new waves, newest
# day first ("Today #1", "Today #2" by listing time), then the relight groups, then the
# older dashboards, whose totals run from 11/06 and overlap the newer tabs.
# id -> (kind, short name, what it covers). kind: "new" wave, "relight" group, "old"
# cumulative dashboard. A registry wave not named here is a new wave named by its
# registry "title" (or its id).
ABOUT = {
    "w1111-elec2028": ("new", "2028 primaries",
                       "Started by you at 7:13 AM ET 11/11 (/market-wave, \"all 724\"): the 2028 "
                       "presidential primaries (D and R) plus the presidential state-results markets."),
    "w1111-statenom": ("new", "Senate/Gov nominees + state races",
                       "Found and listed by the 24/7 wave check at 11:08 AM ET 11/11: 2028 Senate and "
                       "governor nominees, plus state attorney general, secretary of state, treasurer, "
                       "auditor, Lt. governor, insurance and agriculture commissioner races."),
    "w1109-control": ("new", "2030 Senate & House control",
                      "Listed 11/09 (your \"Yes, 200 a side\"): who controls the Senate and the House "
                      "after 2030."),
    "w1111-w28": ("relight", "old 2028 House races",
                  "Not a new wave: set up 11/11 so markets of the 11/06 2028 wave's other series "
                  "(the House races and the rest) are listed again when Kalshi relights them. "
                  "Held positions there unwind on their own."),
    "w1111-pol": ("relight", "11/06 politics",
                  "Not a new wave: set up 11/11 so the 11/06 political wave's markets are listed "
                  "again when Kalshi relights them. Same markets as \"Since 11/06 · politics\"."),
    "w2028": ("old", "every 2028 market",
              "Older dashboard, totals since 11/06 6:26 PM ET: every 2028-wave market, so it "
              "INCLUDES today's 2028 primaries and the old-2028 relight group. Don't add it to them."),
    "pol1106": ("old", "politics",
                "Older dashboard, totals since 11/06: the same political markets as the 11/06 "
                "politics relight group. Don't add the two."),
    "e1104": ("old", "11/04 wave + other listed",
              "Older catch-all, totals since 11/06 9:26 PM ET: every listed market outside the 11/06 "
              "waves. Until it next restarts it ALSO counts a later wave's markets (now: today's "
              "Senate/Gov nominees). Don't add it to today's tabs."),
}
KIND_ORDER = {"new": 0, "relight": 1, "old": 2}


def _et(ts):
    """A registry timestamp in ET (None when unreadable)."""
    try:
        from zoneinfo import ZoneInfo
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None


def name_dashboards(dashes, now):
    """[(id, port, title)] + {id: added} -> [(id, port, title, about)], in tab order."""
    try:
        from zoneinfo import ZoneInfo
        today = now.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:
        today = now.date()
    rows = []
    for idx, (did, port, title, added) in enumerate(dashes):
        kind, short, about = ABOUT.get(did, ("new" if added else "old", title, ""))
        at = _et(added) if added else None
        rows.append({"id": did, "port": port, "kind": kind, "short": short, "about": about,
                     "at": at, "today": bool(at and at.date() == today), "idx": idx})
    # today's new waves in listing order (#1, #2 ...), then older ones newest first
    ts = lambda r: r["at"].timestamp() if r["at"] else 0
    rows.sort(key=lambda r: (KIND_ORDER[r["kind"]], not r["today"],
                             ts(r) if r["today"] else -ts(r), r["idx"]))
    n_today = sorted((r for r in rows if r["kind"] == "new" and r["today"]), key=lambda r: r["at"])
    num = {r["id"]: i for i, r in enumerate(n_today, 1)}
    out = []
    for r in rows:
        if r["kind"] == "new":
            when = f"Today #{num[r['id']]}" if r["today"] else (
                r["at"].strftime("%m/%d") if r["at"] else "Wave")
        elif r["kind"] == "relight":
            when = "Relight group"
        else:
            when = "Since 11/06"
        about = (r["about"] + " " if r["about"] else "") + f"({r['id']}, :{r['port']})"
        out.append((r["id"], r["port"], f"{when} · {r['short']}", about))
    return out


def dashboards(now=None):
    out = [(did, port, title, None) for did, port, title in FIXED]
    try:
        reg = json.load(open(REG, encoding="utf-8"))
        for name, w in (reg.get("waves") or {}).items():
            if w.get("enabled") is False or not w.get("port"):
                continue
            out.append((name, int(w["port"]), (w.get("title") or name), w.get("added") or w.get("go_live")))
    except (OSError, ValueError):
        pass
    return name_dashboards(out, now or datetime.now(timezone.utc))


def fetch(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def pack(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 9)).decode("ascii"), len(raw)


def build():
    now = datetime.now(timezone.utc)
    dashes = []
    for did, port, title, about in dashboards(now):
        e = {"id": did, "port": port, "title": title, "about": about}
        try:
            j = json.loads(fetch(f"http://127.0.0.1:{port}/data"))
        except Exception as ex:          # not running: the page shows it as down
            e.update(ok=False, err=f"dashboard :{port} not answering ({type(ex).__name__})")
            dashes.append(e)
            continue
        j.pop("next", None)              # the dashboard's next read: meaningless in a snapshot
        d = j.get("data") or {}
        e.update(ok=bool(d), err=j.get("err") or "", now=d.get("now"), go_live=d.get("go_live"),
                 totals=d.get("totals") or {}, _j=j)
        dashes.append(e)

    def size():
        for e in dashes:
            if "_j" in e:
                e["z"], e["bytes"] = pack(e["_j"])
        body = {k: v for k, v in doc().items()}
        return len(json.dumps(body, separators=(",", ":")))

    def doc():
        # the page shows `note` under the tab's name: what the tab covers, then any thinning
        return {"v": 1, "built": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dashes": [dict({k: v for k, v in e.items() if k not in ("_j", "about", "note")},
                                note=" ".join(x for x in (e.get("about"), e.get("note")) if x))
                           for e in dashes]}

    n = size()
    for _ in range(12):                   # thin the biggest dashboard until it fits
        if n <= CAP:
            break
        big = max((e for e in dashes if "_j" in e), key=lambda e: len(e.get("z", "")))
        d = big["_j"]["data"]
        if len(d.get("series") or []) > 120:
            d["series"] = d["series"][::2]
            fs = d.get("fill_series")
            if isinstance(fs, dict):         # {t0, step, bid, ask} (2026-11-11): merge bucket pairs, keep totals
                for k in ("bid", "ask"):
                    v = fs.get(k) or []
                    fs[k] = [round(sum(v[i:i + 2]), 1) for i in range(0, len(v), 2)]
                fs["step"] = (fs.get("step") or 300.0) * 2
            else:
                d["fill_series"] = (fs or [])[::2]
            big["note"] = "(Charts thinned to fit the phone page.)"
        else:
            rows = d.get("rows") or []
            keep = max(50, int(len(rows) * 0.7))
            score = lambda r: abs(r.get("trade") or 0) + (r.get("est") or 0) + 1e6 * bool(r.get("pos"))
            d["rows"] = sorted(rows, key=score, reverse=True)[:keep]
            big["rows_total"] = big.get("rows_total") or len(rows)
            big["note"] = (f"(Markets table: the {keep:,} biggest of {big['rows_total']:,}; "
                           f"every market with a position kept.)")
        n = size()
    out = doc()
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    os.replace(tmp, OUT)
    return out, n


def money(v):
    return ("-$" if v < 0 else "$") + f"{abs(v):,.0f}"


def summary(out, n):
    built = datetime.strptime(out["built"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = built.astimezone(ZoneInfo("America/New_York")).strftime("%a %b %d, %I:%M %p ET")
    except Exception:
        et = out["built"]
    print(f"wave pocket built {et} ({out['built']}), {n:,} bytes -> {OUT}")
    tot = {"est": 0.0, "trade": 0.0, "net": 0.0, "rate": 0.0}
    for e in out["dashes"]:
        if not e.get("ok"):
            print(f"  {e['title']} (:{e['port']}): DOWN - {e.get('err')}")
            continue
        t = e.get("totals") or {}
        age = ""
        if e.get("now"):
            try:
                age = f", data {int((built - datetime.fromisoformat(e['now'])).total_seconds() // 60)} min old"
            except ValueError:
                pass
        if (ABOUT.get(e["id"]) or ("new",))[0] != "old":   # the older dashboards overlap the rest
            for k in tot:
                tot[k] += t.get(k) or 0
        print(f"  {e['title']} (:{e['port']}): net {money(t.get('net') or 0)} = est rewards "
              f"{money(t.get('est') or 0)} + trading {money(t.get('trade') or 0)}"
              f" (realized {money(t.get('real') or 0)}, open {money(t.get('open') or 0)}); "
              f"~{money(t.get('rate') or 0)}/day now; quoting {t.get('quoted', 0):,} of {t.get('listed', 0):,}; "
              f"{t.get('fills', 0)} fills{age}" + (f" [{e['note']}]" if e.get("note") else ""))
    print(f"  all registry waves (the 'Since 11/06' dashboards left out: they overlap): net {money(tot['net'])}"
          f" = est rewards {money(tot['est'])} + trading {money(tot['trade'])}; ~{money(tot['rate'])}/day now")
    print(f"  page: {URL}")


# ---------------------------------------------------------------- the page
POCKET_CSS = """
.wp-bar{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
.wp-tab{font:inherit;font-size:13px;padding:5px 10px;border-radius:14px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.wp-tab[aria-pressed="true"]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.wp-tab.down{opacity:.55}
.wp-tab:focus-visible{outline:2px solid var(--est);outline-offset:2px}
.wp-fresh{font-size:12px;color:var(--mut);margin:0 0 8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.wp-chip{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:600;border:1px solid currentColor}
.wp-chip.fresh{color:var(--up)}.wp-chip.stale{color:var(--dn)}.wp-chip.aging{color:var(--trade)}
.wp-note{font-size:12px;color:var(--trade);margin:0 0 6px}
"""

POCKET_HEAD = """<div class="wp-bar" id="wpbar" role="group" aria-label="Dashboards"></div>
<div class="wp-fresh" id="wpfresh">Loading the latest refresh...</div>
<div class="wp-note" id="wpnote" hidden></div>
"""

POCKET_JS = r"""
const POCKET=(()=>{
  let doc=null,sel=null,dbNote='';const memo={};
  try{sel=localStorage.getItem('wp-sel')}catch(e){}
  const seedEl=document.getElementById('wp-seed');
  try{if(seedEl&&seedEl.textContent.trim())doc=JSON.parse(seedEl.textContent)}catch(e){}
  async function unz(b64){
    const bin=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
    if(typeof DecompressionStream==='undefined')throw new Error('this browser cannot unpack the data (needs DecompressionStream)');
    const s=new Blob([bin]).stream().pipeThrough(new DecompressionStream('gzip'));
    return JSON.parse(await new Response(s).text());
  }
  const ago=iso=>{const m=Math.max(0,Math.round((Date.now()-Date.parse(iso))/60000));return m<60?m+' min ago':(m/60).toFixed(1)+' h ago'};
  const etT=iso=>new Date(iso).toLocaleString('en-US',{timeZone:'America/New_York',weekday:'short',hour:'numeric',minute:'2-digit'});
  function fresh(){
    const el=document.getElementById('wpfresh');
    if(!doc){el.textContent=dbNote||'No refresh yet. Ask Claude: "refresh wave pocket".';return}
    const m=(Date.now()-Date.parse(doc.built))/60000;
    const chip=m>60?'<span class="wp-chip stale">Stale</span>':m>30?'<span class="wp-chip aging">Aging</span>':'<span class="wp-chip fresh">Fresh</span>';
    el.innerHTML=chip+'<span>Refreshed '+etT(doc.built)+' ET ('+ago(doc.built)+')'+(m>60?'. Ask Claude: "refresh wave pocket"':'')+'</span>'+(dbNote?'<span>'+dbNote+'</span>':'');
  }
  function tabs(){
    const bar=document.getElementById('wpbar');bar.textContent='';
    for(const d of (doc&&doc.dashes)||[]){
      const b=document.createElement('button');b.type='button';b.className='wp-tab'+(d.ok?'':' down');
      b.textContent=d.title+(d.ok?'':' (down)');b.setAttribute('aria-pressed',String(d.id===sel));
      b.addEventListener('click',()=>{sel=d.id;try{localStorage.setItem('wp-sel',sel)}catch(e){}tabs();show()});
      bar.appendChild(b);
    }
  }
  async function show(){
    fresh();
    const list=(doc&&doc.dashes)||[];if(!list.length)return;
    if(!list.some(d=>d.id===sel))sel=(list.find(d=>d.ok)||list[0]).id;
    tabs();
    const d=list.find(x=>x.id===sel);
    document.getElementById('title').textContent=d.title;
    const note=document.getElementById('wpnote');note.hidden=!d.note;note.textContent=d.note||'';
    const main=document.getElementById('wpmain');
    if(!d.ok||!d.z){main.hidden=true;window.WP_CUR={err:d.err||'dashboard down at the last refresh'};tick();return}
    const key=d.id+'@'+doc.built;let j;
    try{if(!memo[key])memo[key]=await unz(d.z);j=memo[key]}catch(e){j={err:'page: '+e.message}}
    if(d.id!==sel)return;
    main.hidden=!j.data;window.WP_CUR=j;tick();
  }
  function take(d){if(d&&d.built&&(!doc||d.built>doc.built)){doc=d;show()}else fresh()}
  (async()=>{
    show();setInterval(fresh,30000);
    const db=window.claude&&await window.claude.use('db');
    if(!db){dbNote=doc?'(showing the copy saved with the page)':'';fresh();return}
    let t=setTimeout(()=>{dbNote='(can\'t reach the latest refresh yet; showing what this page has)';fresh()},10000);
    db.doc('pocket/latest').onSnapshot(s=>{
      if(!s.metadata.fromCache){clearTimeout(t);dbNote=''}
      if(s.exists)take(s.data());else fresh();
    },e=>{clearTimeout(t);dbNote='(the latest refresh could not be read: '+e.code+')';fresh()});
  })();
  return {};
})();
"""


def page(out_path):
    src = None
    for _, port, _t in FIXED:
        try:
            src = fetch(f"http://127.0.0.1:{port}/").decode("utf-8")
            break
        except Exception:
            continue
    if not src:
        sys.exit("no wave dashboard is answering: start them first (Start-ScheduledTask 'KL pvd-watch keepalive')")

    def rep(a, b):
        nonlocal src
        if src.count(a) != 1:
            sys.exit(f"dashboard page changed: {src.count(a)} matches for {a[:60]!r}")
        src = src.replace(a, b)

    head_end = src.index("<style>")
    src = ('<title>Wave Pocket</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n'
           + src[head_end:])
    rep("body{margin:0;padding:16px;",
        "body{margin:0;padding-block:16px;padding-left:max(16px,env(safe-area-inset-left,0px));"
        "padding-right:max(16px,env(safe-area-inset-right,0px));")
    rep("</style></head><body>", POCKET_CSS + "</style>\n" + POCKET_HEAD)
    rep("<h1>Post-vote dead books</h1>", '<h1 id="title">Wave Pocket</h1>')
    rep("""`<th onclick="sortBy('${k}')">${n}</th>`""", """`<th data-k="${k}">${n}</th>`""")
    rep("async function tick(){\n  try{const r=await fetch('/data',{cache:'no-store'});const j=await r.json();",
        "async function tick(){\n  try{const j=window.WP_CUR;if(!j)return;")
    rep("tick();setInterval(tick,30000);",
        "$('mh').addEventListener('click',e=>{const th=e.target.closest('th');if(th&&th.dataset.k)sortBy(th.dataset.k)});")
    rep('<div class="err" id="err"></div>', '<div class="err" id="err"></div>\n<div id="wpmain">')
    rep("</tbody></table></div>\n<script>\nconst $=", "</tbody></table></div>\n</div>\n<script>\nconst $=")
    rep("</script></body></html>", "</script>")
    seed = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else None
    seed_txt = json.dumps(seed, separators=(",", ":")).replace("</", "<\\/") if seed else ""
    src += ('\n<script type="application/json" id="wp-seed">' + seed_txt + "</script>\n<script>"
            + POCKET_JS + "</script>\n")
    # D is declared with let in the page script; the pocket script runs after it, in the same realm
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(f"page -> {out_path} ({len(src):,} bytes, seed {'built ' + seed['built'] if seed else 'none'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", action="store_true", help="build the data document and print a line a dashboard")
    ap.add_argument("--page", metavar="OUT", help="also write the page (embedding this build) to OUT")
    a = ap.parse_args()
    out, n = build()
    if a.summary or not a.page:
        summary(out, n)
    if a.page:
        page(a.page)


if __name__ == "__main__":
    main()
