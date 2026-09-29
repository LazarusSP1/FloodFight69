#!/usr/bin/env python3
"""Weather Fight feed builder — fetches flood/rain/forecast data for Bangkok
and writes feed.json (one document for the dashboard's db at feed/latest).
Sources: Open-Meteo forecast + GloFAS flood API, ThaiWater (HII) water levels, BMA road-flood sensors, TMD open data API, Google News RSS,
road-flood reports from traffic radio จส.100 (js100.com) and สวพ.91 (fm91bkk.com, also via Google News).
Usage: python3 fetch_feed.py [out.json]
Optional env: ROADS_URL=<article url> forces the flooded-roads source article (Thairath first, else any outlet found via Google News);
PREV_FEED=<path to previous feed JSON> keeps the previous roads block when no new article is found.
Also writes radar/f0.json..f7.json: batch-write each to db collection "radar", doc ids f0..f7.
If roads come back null, keep the previous roads block (see refresh task).
"""
import json, sys, re, html, os, time, urllib.request, urllib.parse, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

LAT, LON = 13.7563, 100.5018
TZ = dt.timezone(dt.timedelta(hours=7))
UA = {"User-Agent": "Mozilla/5.0 WeatherFight/1.0"}

def get(url, timeout=30, tries=3, data=None):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(3 * (k + 1))

def safe(fn, default):
    try:
        return fn()
    except Exception as e:
        print("WARN", fn.__name__, e, file=sys.stderr)
        return default

def weather():
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "timezone": "Asia/Bangkok",
        "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "hourly": "precipitation,precipitation_probability",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
        "past_days": 7, "forecast_days": 7,
    })
    d = json.loads(get("https://api.open-meteo.com/v1/forecast?" + q))
    c = d["current"]
    now = c["time"][:13]
    h = d["hourly"]
    idx = next((i for i, t in enumerate(h["time"]) if t[:13] >= now), 0)
    hourly = [{"t": h["time"][i][11:16], "d": h["time"][i][:10],
               "p": h["precipitation"][i], "pp": h["precipitation_probability"][i]}
              for i in range(idx, min(idx + 48, len(h["time"])))]
    dd = d["daily"]
    daily = [{"date": dd["time"][i], "code": dd["weather_code"][i],
              "tmax": dd["temperature_2m_max"][i], "tmin": dd["temperature_2m_min"][i],
              "rain": dd["precipitation_sum"][i], "pp": dd["precipitation_probability_max"][i]}
             for i in range(len(dd["time"]))]
    return {"current": {"time": c["time"], "temp": c["temperature_2m"], "rh": c["relative_humidity_2m"],
                        "rain": c["precipitation"], "code": c["weather_code"], "wind": c["wind_speed_10m"]},
            "hourly": hourly, "daily": daily}

def river():
    q = urllib.parse.urlencode({"latitude": 13.75, "longitude": 100.49,
                                "daily": "river_discharge,river_discharge_max",
                                "past_days": 14, "forecast_days": 14})
    d = json.loads(get("https://flood-api.open-meteo.com/v1/flood?" + q))["daily"]
    return [{"date": d["time"][i], "q": d["river_discharge"][i], "qmax": d["river_discharge_max"][i]}
            for i in range(len(d["time"]))]

TW_URL = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public/waterlevel_load"
TW_RIVER = ["C.2", "C.13", "C.3", "C.7A", "C.35", "CPY014", "C.12", "CPY015"]
TW_PROV = {"กรุงเทพมหานคร", "นนทบุรี", "ปทุมธานี", "สมุทรปราการ", "สมุทรสาคร", "นครปฐม"}

def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None

def thaiwater():
    d = json.loads(get(TW_URL, timeout=60))
    rows = d["waterlevel_data"]["data"]
    def pack(r):
        s = r.get("station") or {}
        g = r.get("geocode") or {}
        wl, prev = _f(r.get("waterlevel_msl")), _f(r.get("waterlevel_msl_previous"))
        return {"code": s.get("tele_station_oldcode"), "name": (s.get("tele_station_name") or {}).get("th"),
                "prov": (g.get("province_name") or {}).get("th"), "amphoe": (g.get("amphoe_name") or {}).get("th"),
                "basin": ((r.get("basin") or {}).get("basin_name") or {}).get("th"),
                "t": r.get("waterlevel_datetime"), "wl": wl,
                "dwl": round(wl - prev, 2) if wl is not None and prev is not None else None,
                "bank": _f(s.get("min_bank")), "pct": _f(r.get("storage_percent")),
                "lvl": r.get("situation_level"), "diff": _f(r.get("diff_wl_bank")),
                "over": "ล้น" in (r.get("diff_wl_bank_text") or ""), "q": _f(r.get("discharge"))}
    by_code = {}
    area = []
    for r in rows:
        p = pack(r)
        if p["code"]:
            by_code[p["code"]] = p
        if p["prov"] in TW_PROV:
            area.append(p)
    area.sort(key=lambda x: x["pct"] if x["pct"] is not None else -1, reverse=True)
    return {"river": [by_code[c] for c in TW_RIVER if c in by_code], "area": area}

# ---- BMA road-flood sensors (สำนักการระบายน้ำ กทม., weather.bangkok.go.th/flood/) ----
BMA_SENSORS = "https://weather.bangkok.go.th/Flood/PageMap/GetDataTable"
# flood_sub_status: 0 ขัดข้อง, 5 ขัดข้องชั่วคราว (both offline), 1 ปกติ, 2 น้ำท่วมเล็กน้อย, 3 น้ำท่วม

def _msdate(s):
    m = re.search(r"\d{10,}", s or "")
    return dt.datetime.fromtimestamp(int(m.group()) / 1000, TZ).isoformat(timespec="minutes") if m else None

def bma_sensors():
    rows = json.loads(get(BMA_SENSORS, data=b""))  # POST only; GET is 404
    out = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        name = (r.get("flood_shortname") or "").strip()
        if r.get("typesite") == 2 and r.get("tunnel_sub_name"):
            name += " " + r["tunnel_sub_name"].strip()
        out.append({"id": r.get("flood_id"), "name": name.rstrip(" *"),
                    # "*" sites only report steps (5/10/15/20 cm), so 20 means "20 or more"
                    "step": "*" in name, "tunnel": r.get("typesite") == 2,
                    "district": r.get("districtName"), "p": [round(lat, 5), round(lon, 5)],
                    "cm": _f(r.get("flood")), "max": _f(r.get("flood_max")),
                    "st": r.get("flood_sub_status"), "t": _msdate(r.get("site_timestamp")),
                    "since": _msdate(r.get("flood_start")) if (r.get("flood") or 0) > 0 else None})
    return {"fetched": dt.datetime.now(TZ).isoformat(timespec="minutes"),
            "source": "https://weather.bangkok.go.th/flood/", "sites": out}

# ---- flooded roads (BMA road-flood alerts as republished by Thairath) ----
import os, time
TR_RSS = "https://www.thairath.co.th/rss/news"
TR_SITEMAP = "https://www.thairath.co.th/sitemap-daily.xml"
ROAD_TITLE = re.compile(r"(เลี่ยง|ท่วม).*?\d+\s*(เส้นทาง|ถนน|สาย|จุด)|\d+\s*(เส้นทาง|ถนน|สาย)\s*.*ท่วม")
GEO_BOX = (100.30, 13.45, 100.98, 14.15)  # lon_min, lat_min, lon_max, lat_max

def _road_candidates():
    cands = []
    try:
        r = ET.fromstring(get(TR_RSS))
        for i in r.iter("item"):
            cands.append((i.findtext("title") or "", i.findtext("link") or ""))
    except Exception as e:
        print("WARN thairath rss", e, file=sys.stderr)
    try:
        sm = get(TR_SITEMAP).decode("utf-8", "ignore")
        for loc, title in re.findall(r"<loc>(https://www\.thairath\.co\.th/news/[^<]+)</loc>.*?<image:title><!\[CDATA\[(.*?)\]\]>", sm, re.S):
            cands.append((title, loc))
    except Exception as e:
        print("WARN thairath sitemap", e, file=sys.stderr)
    seen, out = set(), []
    for title, link in cands:
        if link in seen or "ท่วม" not in title or not ROAD_TITLE.search(title):
            continue
        seen.add(link)
        m = re.search(r"/(\d{6,})", link)
        out.append((int(m.group(1)) if m else 0, title, link))
    out.sort(reverse=True)  # newest article id first
    return out

def _article(url):
    page = get(url).decode("utf-8", "ignore")
    body, published, headline = "", "", ""
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            j = json.loads(block)
        except Exception:
            continue
        for obj in (j if isinstance(j, list) else [j]):
            if isinstance(obj, dict) and obj.get("articleBody"):
                body, published, headline = obj["articleBody"], obj.get("datePublished", ""), obj.get("headline", "")
    return body, published, headline

def parse_roads(body):
    items, pos, n = [], 0, 1
    starts = []
    while True:
        k = body.find(f"{n}. ถ.", pos)
        if k < 0:
            break
        starts.append(k + len(f"{n}. "))
        pos, n = k + 3, n + 1
    for i, s in enumerate(starts):
        end = starts[i + 1] - len(f"{i + 2}. ") if i + 1 < len(starts) else len(body)
        chunk = body[s:end].strip()
        m = re.match(r"(ถ\.[^:]*?)(?:\s*ท่วมสูง\s*(\d+)\s*ซม\.[^ช]*)?ช่วงน้ำท่วม:\s*(.*)$", chunk, re.S)
        if not m:
            m2 = re.match(r"(ถ\.\S+(?:\s\S+)?)\s*ท่วมสูง\s*(\d+)\s*ซม", chunk)
            if m2:
                items.append({"road": m2.group(1).strip(), "depth": int(m2.group(2)), "segs": []})
            continue
        road, depth, rest = m.group(1).strip(), m.group(2), m.group(3)
        md = re.search(r"ท่วมสูง\s*(\d+)\s*ซม\.?\s*(หรือมากกว่า)?", rest)
        if md and not depth:
            depth = md.group(1)
        rest = re.sub(r"\s*ท่วมสูง\s*\d+\s*ซม\.?\s*(หรือมากกว่า)?", "", rest)
        rest = re.split(r"\s{2,}|\n", rest.strip())[0]
        segs = []
        for part in [p.strip() for p in rest.split("·") if p.strip()]:
            mm = re.match(r"จาก\s+(.+?)\s+ถึง\s+(.+)$", part)
            if mm:
                segs.append({"from": mm.group(1).strip(), "to": mm.group(2).strip()})
            else:
                segs.append({"near": re.sub(r"^บริเวณใกล้\s*", "", part).strip()})
        items.append({"road": road, "depth": int(depth) if depth else None, "segs": segs})
    return items

_geo_cache = {}
_GEO_CACHE_PATH = os.environ.get("GEO_CACHE")
if _GEO_CACHE_PATH and os.path.exists(_GEO_CACHE_PATH):
    try:
        _geo_cache.update(json.load(open(_GEO_CACHE_PATH, encoding="utf-8")))
    except Exception:
        pass
def _norm(q):
    q = re.sub(r"ซ\.\s*(?=\d)", "ซอย ", q)
    q = re.sub(r"ซ\.\s*", "ซอย", q)
    q = re.sub(r"\bถ\.\s*", "ถนน", q)
    return re.sub(r"\s+", " ", q).strip()

def geocode(q):
    q = _norm(q)
    if q in _geo_cache:
        return _geo_cache[q]
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "format": "json", "limit": 1, "countrycodes": "th", "bounded": 1,
        "viewbox": f"{GEO_BOX[0]},{GEO_BOX[3]},{GEO_BOX[2]},{GEO_BOX[1]}", "q": q})
    res = None
    try:
        time.sleep(1.1)  # Nominatim usage policy: max 1 request/second
        r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "WeatherFightDashboard/1.0"}), timeout=20).read())
        if r:
            res = [round(float(r[0]["lat"]), 5), round(float(r[0]["lon"]), 5)]
    except Exception as e:
        print("WARN geocode", q, e, file=sys.stderr)
    _geo_cache[q] = res
    return res

def _geo_point(name, road):
    rn = _norm(road)
    for q in ([name] if name.startswith(("ซ.", "ถ.", "แยก", "ซอย", "ถนน")) else []) + [f"{name} {rn}", name]:
        p = geocode(q)
        if p:
            return p
    return None

SEG_SPLIT = re.compile(r"\s*·\s*|\s+-\s+|\s+(?=จาก\s)|\s+(?=บริเวณใกล้\s)")
def parse_roads_generic(body):
    """Numbered BMA road-flood list in any outlet's wording:
    'N. ถ.X ... ช่วงน้ำท่วม: จาก A ถึง B · บริเวณใกล้ C ... ท่วมสูง D ซม.'"""
    body = re.sub(r"\s+", " ", body)
    starts, pos, n = [], 0, 1
    miss = 0
    while miss < 3:
        m = re.compile(rf"{n}\.\s*(?=ถ\.|ถนน)").search(body, pos)
        if not m:
            n, miss = n + 1, miss + 1  # tolerate an item the outlet worded differently
            continue
        starts.append((m.start(), m.end())); pos, n, miss = m.end(), n + 1, 0
    items = []
    for i, (s0, s) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else min(len(body), s + 600)
        chunk = body[s:end]
        mm = re.match(r"((?:ถ\.|ถนน)\s*[^\s:]+(?:\s(?:\d+|ร\.\d+))?)", chunk)
        if not mm:
            continue
        road = re.sub(r"^ถนน\s*", "ถ.", mm.group(1)).strip()
        road = re.sub(r"(ช่วงน้ำท่วม|ท่วมสูง).*$", "", road).strip()
        depths = [int(d) for d in re.findall(r"ท่วมสูง\s*(\d+)\s*ซม", chunk)]
        segtxt = ""
        ms = re.search(r"ช่วงน้ำท่วม\s*:?\s*(.*)", chunk)
        if ms:
            segtxt = re.split(r"\s*(?:ท่วมสูง|น้ำท่วม\s*\d+\s*จุดวัด|จุดวัด)", ms.group(1))[0]
        segs = []
        for part in [p.strip(" ,") for p in SEG_SPLIT.split(segtxt) if p.strip(" ,")]:
            m2 = re.match(r"จาก\s+(.+?)\s+ถึง\s+(.+)$", part)
            if m2:
                segs.append({"from": m2.group(1).strip(), "to": m2.group(2).strip()})
            elif part.startswith("บริเวณใกล้"):
                segs.append({"near": part.replace("บริเวณใกล้", "", 1).strip()})
        items.append({"road": road, "depth": max(depths) if depths else None, "segs": segs})
    return items

def parse_roads_unnumbered(body):
    """BMA 'N ถนนที่ยังมีน้ำท่วมขัง(สูง)' summary: road names run together, each
    optionally followed by 'ช่วง A ถึง B' / 'บริเวณ C', until the next section."""
    t = re.sub(r"\s+", " ", body)
    m = re.search(r"\d+\s*(?:ถนน|สาย|เส้นทาง)\s*ที่ยัง(?:มี)?น้ำท่วม(?:ขัง)?(?:สูง)?\s*:?"
                  r"|(?:ที่)?ควรหลีกเลี่ยง\s*(?:จำนวน\s*)?\d+\s*(?:ถนน|สาย|เส้นทาง)\s*(?:ได้แก่|คือ)?\s*:?"
                  r"|\d+\s*(?:ถนน|สาย|เส้นทาง)\s*ที่ควรหลีกเลี่ยง\s*(?:ได้แก่|คือ)?\s*:?", t)
    if not m:
        return []
    seg = t[m.end():m.end() + 2500]
    seg = re.split(r"\d+\s*(?:ถนน|สาย|เส้นทาง)\s*(?:ที่)?คืนผิว|ถนนที่คืนผิว|คืนผิวจราจร(?:ได้)?แล้ว\s*:|ภาพ\s*:|อ่านข่าว|ข่าวที่เกี่ยวข้อง|NEWS UPDATE", seg)[0]
    # a new item starts at "ถนน…" unless it continues a range/landmark ("ช่วงถนน", "ถึงถนน", "บริเวณถนน")
    parts = [p.strip(" ,") for p in re.split(r"(?<!ช่วง)(?<!ถึง)(?<!ถึง )(?<!บริเวณ)(?<!แยก)(?<!ตัด)(?=ถนน[^\s])", seg)
             if p.strip(" ,").startswith("ถนน")]
    items = []
    for p in parts[:40]:
        mm = re.match(r"ถนน(\S+?)(?=\s|ช่วง|บริเวณ|$)\s*(.*)", p)
        if not mm or len(mm.group(1)) > 25:
            break  # ran into prose after the list
        if len(mm.group(2)) > 160 and "ช่วง" not in mm.group(2)[:20] and "บริเวณ" not in mm.group(2)[:20]:
            items.append({"road": "ถ." + mm.group(1), "depth": None, "segs": []})
            break
        road, rest = "ถ." + mm.group(1), mm.group(2).strip()
        segs = []
        rest = re.split(r"\s(?=ประชาชน|เจ้าหน้าที่|ขณะที่|ทั้งนี้|โดยเฉพาะ|อย่างไรก็ตาม)", rest)[0]
        rest = re.split(r"\s(?=[^\s]{0,6}(?:ที่|ซึ่ง|โดย|ทั้งนี้|อย่างไรก็ตาม))", rest)[0] if len(rest) > 160 else rest
        for piece in re.split(r"\s*และ\s*(?=บริเวณ|ช่วง)|\s*,\s*", rest):
            piece = piece.strip()
            m2 = re.match(r"ช่วง\s*(.+?)\s*ถึง\s*(.+)$", piece)
            if m2:
                segs.append({"from": re.sub(r"^ตัด", "", m2.group(1).strip()), "to": m2.group(2).strip()})
            elif piece.startswith(("บริเวณ", "ช่วง")) and len(piece) > 6:
                segs.append({"near": re.sub(r"^(บริเวณ|ช่วง)\s*(หน้า)?", "", piece).strip()})
        items.append({"road": road, "depth": None, "segs": segs})
    return items

GN_ROAD_QUERIES = ["กทม. เลี่ยง เส้นทาง น้ำท่วมขัง when:1d", "ถนน น้ำท่วมขัง กทม. เส้นทาง when:1d"]

def _gn_resolve(link):
    """Resolve a news.google.com/rss/articles/... link to the publisher URL."""
    aid = link.split("/articles/")[1].split("?")[0]
    pg = get(f"https://news.google.com/rss/articles/{aid}").decode("utf-8", "ignore")
    sg = re.search(r'data-n-a-sg="([^"]+)"', pg).group(1)
    ts = re.search(r'data-n-a-ts="([^"]+)"', pg).group(1)
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], aid, int(ts), sg]
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", json.dumps(inner)]]]))
    req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body.encode(),
                                 headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    r = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    return json.loads(json.loads(r.split("\n\n")[1])[0][2])[1]

def _gn_road_candidates():
    out, seen = [], set()
    for q in GN_ROAD_QUERIES:
        try:
            x = ET.fromstring(get("https://news.google.com/rss/search?" + urllib.parse.urlencode(
                {"q": q, "hl": "th", "gl": "TH", "ceid": "TH:th"})))
        except Exception as e:
            print("WARN gnews roads", e, file=sys.stderr); continue
        for i in x.iter("item"):
            title = html.unescape(i.findtext("title") or "")
            if "ท่วม" not in title or not ROAD_TITLE.search(title) or title in seen:
                continue
            seen.add(title)
            try:
                ts = parsedate_to_datetime(i.findtext("pubDate")).timestamp()
            except Exception:
                ts = 0
            out.append((ts, title, i.findtext("link")))
    out.sort(reverse=True)
    return out

def _page_text(url):
    """articleBody from JSON-LD when present, else the page's paragraph/list text."""
    page = get(url).decode("utf-8", "ignore")
    body, published, headline = "", "", ""
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            j = json.loads(block)
        except Exception:
            continue
        for obj in (j if isinstance(j, list) else [j]):
            if isinstance(obj, dict) and obj.get("articleBody"):
                body, published, headline = obj["articleBody"], obj.get("datePublished", ""), obj.get("headline", "")
    s = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    paras = " ".join(html.unescape(re.sub(r"<[^>]+>", "", x)) for x in re.findall(r"<(?:p|li)[^>]*>(.*?)</(?:p|li)>", s, re.S))
    if not published:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"|article:published_time"\s+content="([^"]+)"', page)
        published = (m.group(1) or m.group(2)) if m else ""
    return body, paras, published, headline

def _best_items(*texts):
    best = []
    for t in texts:
        if not t:
            continue
        for fn in (parse_roads, parse_roads_generic, parse_roads_unnumbered):
            try:
                items = fn(t)
            except Exception:
                items = []
            if len(items) > len(best):
                best = items
    merged = {}
    for it in best:  # merge duplicate roads
        if it["road"] in merged:
            merged[it["road"]]["segs"] += it["segs"]
            merged[it["road"]]["depth"] = max(filter(None, [merged[it["road"]]["depth"], it["depth"]]), default=None)
        else:
            merged[it["road"]] = it
    return list(merged.values())

def roads():
    url = os.environ.get("ROADS_URL")
    if url:
        cands = [(0, "", url, False)]
    else:
        # Thairath directly (blocked from some hosts, e.g. GitHub runners), then any outlet via Google News
        cands = [(0, t, l, False) for _, t, l in _road_candidates()[:2]] + \
                [(0, t, l, True) for _, t, l in _gn_road_candidates()[:6]]
    for _, title, link, via_gn in cands:
        try:
            if via_gn:
                link = _gn_resolve(link)
                if "facebook.com" in link:
                    continue
            body, paras, published, headline = _page_text(link)
        except Exception as e:
            print("WARN article", link[:80], e, file=sys.stderr)
            continue
        items = _best_items(body, paras)
        print(f"roads candidate: {len(items)} roads · {(headline or title)[:70]} · {link[:70]}", file=sys.stderr)
        if len(items) < 5:
            continue
        mt = re.search(r"(?:เวลา|รอบ)\s*(\d{1,2}[.:]\d{2})\s*น\.", body or paras)
        road_pts = {}
        for it in items:
            if it["road"] not in road_pts:
                road_pts[it["road"]] = geocode(it["road"] + " กรุงเทพมหานคร")
            for s in it["segs"]:
                if "near" in s:
                    s["p"] = _geo_point(s["near"], it["road"])
                else:
                    s["a"] = _geo_point(s["from"], it["road"])
                    s["b"] = _geo_point(s["to"], it["road"])
            it["p"] = road_pts[it["road"]]
        print(f"roads source: {link}", file=sys.stderr)
        return {"source": link, "headline": headline or re.sub(r"\s+-\s+[^-]+$", "", title), "published": published,
                "asof": mt.group(1).replace(".", ":") if mt else "", "items": items}
    return None

# ---- live road-flood reports from traffic radio (จส.100, สวพ.91) ----
# flooding, and the receding-water updates that follow it ("น้ำลดแล้ว", "คืนผิวจราจร", "สัญจรได้")
FLOOD_RE = re.compile(r"ท่วม|น้ำขัง|น้ำยังสูง|น้ำสูง|ระดับน้ำ|รอการระบาย|สถานการณ์น้ำ|น้ำ(?:เริ่ม)?ลด|คืนผิว|รถเล็ก(?:ห้าม|ไม่สามารถ)?ผ่าน")
# must name a road/traffic situation, not just mention the flood
ROAD_RE = re.compile(r"ถนน|ถ\.|ซอย|ซ\.\S|แยก|สะพาน|ทางด่วน|ทางพิเศษ|ขาเข้า|ขาออก|ช่องทาง|ปิดจราจร|คืนผิว|รถเล็ก")
NOT_ROAD = re.compile(r"ศูนย์พักพิง|บริจาค|ถุงยังชีพ|เยียวยา|ประชุม|นายกฯ|ครม\.|ประกัน|คปภ|ออมสิน|สินเชื่อ|ประปา|กปน|"
                      r"การไฟฟ้า|MEA|PEA|โรค|แพทย์|ขยายเวลา|ให้บริการฟรี|ลงพื้นที่ช่วย|รฟท|ทางรถไฟ|ขบวนรถ|ศาล|จำคุก|คดี|ผู้ต้องหา")
TH_MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม",
             "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
JS100_TRAFFIC = "https://www.js100.com/en/site/traffic"
JS100_NEWS = "https://www.js100.com/en/site/news"
FM91_HOME = "https://www.fm91bkk.com/"

def _txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()

TH_ABBR = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
EN_MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

def _year(y):
    """Buddhist or Christian era, 2 or 4 digits -> Christian year."""
    y = int(y)
    if y < 100:
        y += 2500 if y >= 43 else 2000  # '69' = 2569 BE; '26' = 2026
    return y - 543 if y > 2400 else y

def _th_date(s, now=None):
    """Date/time as js100.com prints it -> ISO (+07:00), '' if unreadable. Handles
    '25  กันยายน 2569,   14:12น.', '28 ก.ย. 69 14:12', '28/09/2569 14:12', 'Sep 28, 2026 14:12',
    'วันนี้ 14:12', 'เมื่อวาน 14:12' and a bare '14:12น.' (today, or yesterday if that is still ahead)."""
    s = re.sub(r"\s+", " ", s or "").strip()
    now = now or dt.datetime.now(TZ)
    tm = re.search(r"(\d{1,2})[:.](\d{2})(?!\d)", s)
    if not tm:
        return ""
    hh, mi = int(tm.group(1)), int(tm.group(2))
    d = mo = y = None
    m = re.search(r"(\d{1,2}) ?([ก-๙]+\.?[ก-๙]*\.?) ?(\d{2,4}(?![:.]\d))?", s)
    if m and (m.group(2) in TH_MONTHS or m.group(2) in TH_ABBR):
        d = int(m.group(1)); mo = (TH_MONTHS + TH_ABBR).index(m.group(2)) % 12 + 1
        y = _year(m.group(3)) if m.group(3) else now.year
    elif (m := re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)):
        d, mo, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
    elif (m := re.search(r"([A-Za-z]{3})[a-z]* (\d{1,2}),? (\d{4})|(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})", s)) and \
            (m.group(1) or m.group(5)).lower() in EN_MON:
        mon = (m.group(1) or m.group(5)).lower()
        d, mo, y = int(m.group(2) or m.group(4)), EN_MON.index(mon) + 1, int(m.group(3) or m.group(6))
    else:
        day = now.date() - dt.timedelta(days=1 if "เมื่อวาน" in s else 0)
        t = dt.datetime(day.year, day.month, day.day, hh, mi, tzinfo=TZ)
        if "วันนี้" not in s and "เมื่อวาน" not in s and t > now + dt.timedelta(minutes=10):
            t -= dt.timedelta(days=1)  # bare time later than now = yesterday
        if re.search(r"\d{1,2} ?[ก-๙A-Za-z]", s[:tm.start()]) and "วันนี้" not in s and "เมื่อวาน" not in s:
            return ""  # has a date part we could not read: don't guess
        return t.isoformat(timespec="minutes")
    try:
        return dt.datetime(y, mo, d, hh, mi, tzinfo=TZ).isoformat(timespec="minutes")
    except ValueError:
        return ""

def _depth(t):
    m = re.search(r"(\d{1,3})\s*(?:-|–|~|ถึง)\s*(\d{1,3})\s*(?:ซม|เซนติเมตร)", t)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d{1,3})\s*(?:ซม|เซนติเมตร)", t)
    return int(m.group(1)) if m else None

def _is_flood(t):
    return bool(FLOOD_RE.search(t)) and bool(ROAD_RE.search(t)) and not NOT_ROAD.search(t)

def parse_js100_traffic(page):
    """js100.com/en/site/traffic: <ul id="latest_traffic_list"><li><h4>date</h4><p>text</p></li>"""
    m = re.search(r'id="latest_traffic_list".*?</ul>', page, re.S)
    out = []
    for h4, p in re.findall(r"<li>\s*<h4>(.*?)</h4>\s*<p>(.*?)</p>", m.group(0) if m else "", re.S):
        t = _txt(p)
        if t:
            out.append({"src": "จส.100", "text": t, "ts": _th_date(_txt(h4)), "url": JS100_TRAFFIC, "raw_date": _txt(h4)[:60]})
    return out

def parse_js100_news(page):
    out, seen = [], set()
    for mm in re.finditer(r'<a href="(https://www\.js100\.com/en/site/news/view/(\d+))"[^>]*>([^<]{6,})</a>', page):
        t = _txt(mm.group(3))
        if mm.group(2) in seen or t == "อ่านต่อ":
            continue
        seen.add(mm.group(2))
        d = re.search(r'class="news_date">(.*?)</h4>', page[mm.start():mm.start() + 2500], re.S)
        out.append({"src": "จส.100", "text": t, "ts": _th_date(_txt(d.group(1))) if d else "", "url": mm.group(1)})
    return out

def parse_fm91_links(page):
    """Any /newsarticle/<id> link with a headline-length text (the site has no RSS)."""
    out, seen = [], set()
    for href, nid, inner in re.findall(r'<a[^>]+href="((?:https?://(?:www\.)?fm91bkk\.com)?/newsarticle/(\d+))"[^>]*>(.*?)</a>', page, re.S):
        t = _txt(inner)
        if nid in seen or len(t) < 12:
            continue
        seen.add(nid)
        out.append({"src": "สวพ.91", "text": t, "ts": "", "url": "https://www.fm91bkk.com/newsarticle/" + nid, "id": int(nid)})
    return out

def _gn_site(site, src):
    items = gnews(f"site:{site} (น้ำท่วม OR ท่วมขัง OR น้ำขัง OR ระดับน้ำ)", 30)
    return [{"src": src, "text": it["title"], "ts": it["ts"], "url": it["url"]} for it in items]

def road_reports():
    """Flood reports from จส.100 and สวพ.91 in the last 24 h, newest first.
    None when every source failed (main() then keeps the previous list)."""
    got, ok = [], False
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(hours=24)).isoformat(timespec="minutes")
    for name, fn in [
        ("js100 traffic", lambda: parse_js100_traffic(get(JS100_TRAFFIC, timeout=20, tries=2).decode("utf-8", "ignore"))),
        ("js100 news", lambda: parse_js100_news(get(JS100_NEWS, timeout=20, tries=2).decode("utf-8", "ignore"))),
        ("fm91 home", lambda: parse_fm91_links(get(FM91_HOME, timeout=20, tries=2).decode("utf-8", "ignore"))),
        ("gnews fm91", lambda: _gn_site("fm91bkk.com", "สวพ.91")),
        ("gnews js100", lambda: _gn_site("js100.com", "จส.100")),
    ]:
        try:
            items = fn(); got += items; ok = True
            fl = [x for x in items if _is_flood(x["text"])]
            print(f"reports {name}: {len(items)} items, {len(fl)} road-flood, "
                  f"{sum(1 for x in fl if not x['ts'])} undated, {sum(1 for x in fl if x['ts'] and x['ts'] >= cutoff)} in last 24 h, "
                  f"newest {max((x['ts'] for x in items if x['ts']), default='-')}", file=sys.stderr)
            if name.startswith("js100"):
                for x in sorted(items, key=lambda x: x["ts"], reverse=True)[:4]:
                    print(f"  {'KEEP' if _is_flood(x['text']) else 'drop'} {x['ts'][5:16]} {x['text'][:90]}", file=sys.stderr)
            bad = [x["raw_date"] for x in items if not x["ts"] and x.get("raw_date")]
            if bad:
                print(f"reports {name}: unreadable dates e.g. {bad[:3]!r}", file=sys.stderr)
        except Exception as e:
            print("WARN reports", name, e, file=sys.stderr)
    if not ok:
        return None
    # FM91 homepage links carry no time; take it from the matching Google News item, else skip
    gn_ts = {re.sub(r"\W", "", x["text"])[:40]: x["ts"] for x in got if x["ts"]}
    out, seen = [], set()
    for x in got:
        key = re.sub(r"\W", "", x["text"])[:40]
        x["ts"] = x["ts"] or gn_ts.get(key, "")
        if key in seen or not x["ts"] or x["ts"] < cutoff or not _is_flood(x["text"]):
            continue
        seen.add(key)
        out.append({"src": x["src"], "text": x["text"][:400], "ts": x["ts"], "url": x["url"], "depth": _depth(x["text"])})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:40]

def merge_reports(new, prev):
    """This run's reports plus earlier ones still under 24 h old: the sources only list their
    newest items, so a report would otherwise vanish as soon as it scrolls off their pages.
    The filter is re-applied so older non-road items drop out too."""
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(hours=24)).isoformat(timespec="minutes")
    out, seen = [], set()
    for x in (new or []) + (prev or []):
        key = re.sub(r"\W", "", x.get("text", ""))[:40]
        if key in seen or not x.get("ts") or x["ts"] < cutoff or not _is_flood(x["text"]):
            continue
        seen.add(key)
        out.append(x)
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:40]

# ---- rain radar (TMD Suvarnabhumi 120 km loop) ----
import base64, io
RADAR_GIF = "https://weather.tmd.go.th/svp/svp120loop.gif"
RADAR_FRAMES = 8

def radar(out_dir=None):
    out_dir = out_dir or os.environ.get("RADAR_DIR", "radar")
    """Writes the most recent loop frames as radar/f<i>.json ({i, n, img: data-URI webp, fetched})
    for the dashboard's db collection "radar". Returns metadata for the main feed doc."""
    from PIL import Image, ImageSequence
    im = Image.open(io.BytesIO(get(RADAR_GIF, timeout=60)))
    frames = [f.convert("RGB") for f in ImageSequence.Iterator(im)][-RADAR_FRAMES:]
    os.makedirs(out_dir, exist_ok=True)
    fetched = dt.datetime.now(TZ).isoformat(timespec="minutes")
    for i, fr in enumerate(frames):
        b = io.BytesIO()
        fr.save(b, "WEBP", quality=58, method=6)
        doc = {"i": i, "n": len(frames), "fetched": fetched,
               "img": "data:image/webp;base64," + base64.b64encode(b.getvalue()).decode()}
        with open(os.path.join(out_dir, f"f{i}.json"), "w") as fh:
            json.dump(doc, fh)
    return {"frames": len(frames), "fetched": fetched, "source": "https://weather.tmd.go.th/svp120loop.php"}

def clean(s):
    return re.sub(r"\s+\n", "\n", (s or "").replace("\r", "")).strip()

def tmd_warnings():
    r = ET.fromstring(get("https://data.tmd.go.th/api/WeatherWarningNews/v2/?uid=api&ukey=api12345"))
    out = []
    for w in r.iter("Warning"):
        out.append({"no": w.findtext("IssueNo"), "title": clean(w.findtext("TitleThai")),
                    "headline": clean(w.findtext("HeadlineThai"))[:1200],
                    "start": w.findtext("EffectStartDate"), "end": w.findtext("EffectEndDate"),
                    "announced": w.findtext("AnnounceDate"), "url": w.findtext("WebUrlThai")})
    return out

def tmd_daily():
    r = ET.fromstring(get("https://data.tmd.go.th/api/DailyForecast/v2/?uid=api&ukey=api12345"))
    f = r.find("DailyForecast")
    regions = {x.findtext("RegionNameThai"): clean(x.findtext("DescriptionThai")) for x in f.iter("RegionForecast")}
    return {"date": clean(f.findtext("Date")), "overall": clean(f.findtext("OverallDescriptionThai"))[:1500],
            "bkk": regions.get("กรุงเทพและปริมณฑล", ""), "central": regions.get("ภาคกลาง", ""),
            "east": regions.get("ภาคตะวันออก", "")}

def gnews(query, n=20):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query + " when:3d", "hl": "th", "gl": "TH", "ceid": "TH:th"})
    r = ET.fromstring(get(url))
    items = []
    for i in r.iter("item"):
        src = i.findtext("source") or ""
        title = html.unescape(i.findtext("title") or "")
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            ts = parsedate_to_datetime(i.findtext("pubDate")).astimezone(TZ).isoformat(timespec="minutes")
        except Exception:
            ts = ""
        items.append({"title": title.strip(), "src": src, "url": i.findtext("link"), "ts": ts})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:n]

def news():
    feeds = {"shelter": "ศูนย์พักพิง OR อพยพ OR \"ปภ. แจ้งเตือน\" OR \"ปภ. เตือน\"",
             "flood": "น้ำท่วม กรุงเทพ", "rain": "ฝนตกหนัก", "forecast": "พยากรณ์อากาศ กรมอุตุนิยมวิทยา"}
    out, seen = {}, set()
    for k, q in feeds.items():
        got = safe(lambda: gnews(q, 30), None)
        if got is None:  # fetch failed: main() carries over the previous list
            out[k] = None
            continue
        lst = []
        for it in got:
            key = re.sub(r"\W", "", it["title"])[:40]
            if key in seen:
                continue
            seen.add(key)
            lst.append(it)
        out[k] = lst[:20]
    return out

def main():
    doc = {
        "updatedAt": dt.datetime.now(TZ).isoformat(timespec="minutes"),
        "weather": safe(weather, None),
        "river": safe(river, []),
        "thaiwater": safe(thaiwater, None),
        "bma": safe(bma_sensors, None),
        "roads": safe(roads, None),
        "reports": safe(road_reports, None),
        "radar": safe(radar, None),
        "tmd": {"warnings": safe(tmd_warnings, None), "daily": safe(tmd_daily, None)},
        "news": news(),
    }
    prev_path = os.environ.get("PREV_FEED")
    if prev_path and os.path.exists(prev_path):
        try:
            prev = json.load(open(prev_path, encoding="utf-8"))
            prev = prev.get("data", prev) if isinstance(prev, dict) else {}
            if doc["roads"] is None and prev.get("roads"):
                doc["roads"] = dict(prev["roads"], carried=True)
            # keep the last good block when a source fails this run
            for k in ("weather", "river", "thaiwater", "bma", "radar"):
                if not doc.get(k) and prev.get(k):
                    doc[k] = dict(prev[k], stale=True) if isinstance(prev[k], dict) else prev[k]
            if not (doc.get("tmd") or {}).get("daily") and (prev.get("tmd") or {}).get("daily"):
                doc["tmd"]["daily"] = prev["tmd"]["daily"]
            # None = fetch failed (an empty list is a real "nothing new" answer)
            if doc["tmd"]["warnings"] is None:
                doc["tmd"]["warnings"] = (prev.get("tmd") or {}).get("warnings") or []
            doc["reports"] = merge_reports(doc["reports"], prev.get("reports"))
            for k, v in doc["news"].items():
                if v is None:
                    doc["news"][k] = (prev.get("news") or {}).get(k) or []
        except Exception as e:
            print("WARN prev feed", e, file=sys.stderr)
    if doc["tmd"]["warnings"] is None:
        doc["tmd"]["warnings"] = []
    if doc["reports"] is None:
        doc["reports"] = []
    doc["news"] = {k: v or [] for k, v in doc["news"].items()}
    if _GEO_CACHE_PATH:
        json.dump(_geo_cache, open(_GEO_CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    out = sys.argv[1] if len(sys.argv) > 1 else "feed.json"
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    open(out, "w", encoding="utf-8").write(s)
    print(f"wrote {out}: {len(s.encode())} bytes; news " +
          ", ".join(f"{k}={len(v)}" for k, v in doc["news"].items()) +
          f"; reports={len(doc['reports'])}; bma_sites={len((doc['bma'] or {}).get('sites', []))}; radar_frames={(doc['radar'] or {}).get('frames', 0)}; roads={len((doc['roads'] or {}).get('items', []))}{' (carried over)' if (doc['roads'] or {}).get('carried') else ''}")

if __name__ == "__main__":
    main()
