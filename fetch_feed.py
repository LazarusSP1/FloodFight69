#!/usr/bin/env python3
"""Weather Fight feed builder — fetches flood/rain/forecast/air-quality data for Bangkok, Rayong and Chiang Mai
and writes feed.json (one document for the dashboard's db at feed/latest).
Sources: Google Weather API (when keyed) over Open-Meteo forecast + GloFAS flood API, Air4Thai (PCD) + Open-Meteo air quality, TMD radars (Suvarnabhumi, Rayong, Chiang Mai), ThaiWater (HII) water levels, rain gauges and reservoirs, BMA road-flood sensors,
TMD open data API, Longdo Event flood reports (iTIC), Google News RSS, road-flood reports from traffic radio จส.100 (js100.com) and สวพ.91 (fm91bkk.com, also via Google News).
Usage: python3 fetch_feed.py [out.json]
Optional env: ROADS_URL=<article url> forces the flooded-roads source article (Thairath first, else any outlet found via Google News);
PREV_FEED=<path to previous feed JSON> keeps the previous roads block when no new article is found;
BMA_FILE=<path to bma.json> is used when the BMA site blocks this machine (the file is uploaded by bma_push.py);
GOOGLE_WEATHER_API_KEY=<key> takes current conditions and forecasts from the Google Weather API (Open-Meteo stays the fallback).
Local runs also read KEY=value lines from .env.
Also writes radar/, radar_ry/, radar_cm/ f0.json..f7.json: batch-write each to db collection "radar", doc ids f0..f7.
If roads come back null, keep the previous roads block (see refresh task).
"""
import json, sys, re, html, os, ssl, time, urllib.request, urllib.parse, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

def _load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Local runs: KEY=value lines in the .env next to this script fill in unset variables (GitHub Actions passes secrets as env instead)."""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
_load_dotenv()

LAT, LON = 13.7563, 100.5018
TZ = dt.timezone(dt.timedelta(hours=7))
UA = {"User-Agent": "Mozilla/5.0 WeatherFight/1.0"}

def get(url, timeout=30, tries=3, data=None, ctx=None):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA, data=data)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
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
        "past_days": 7, "forecast_days": 10,
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

# ---- Google Weather API (optional, GOOGLE_WEATHER_API_KEY) ----
GW_KEY = os.environ.get("GOOGLE_WEATHER_API_KEY", "").strip()
GW_URL = "https://weather.googleapis.com/v1/"
GW_ERR = {}  # place -> why Google failed this run (goes into feed.json as google_err)

def _gw(path, lat, lon, **q):
    """GET one Google Weather endpoint, following nextPageToken. The key travels in a header, never in the URL,
    so it cannot leak into logs or error messages."""
    q.update({"location.latitude": lat, "location.longitude": lon, "languageCode": "th"})
    pages, token = [], None
    while len(pages) < 5:
        url = GW_URL + path + "?" + urllib.parse.urlencode(dict(q, **({"pageToken": token} if token else {})))
        req = urllib.request.Request(url, headers={"X-Goog-Api-Key": GW_KEY, "User-Agent": UA["User-Agent"]})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read())["error"]["message"]
            except Exception:
                msg = e.reason
            raise RuntimeError(f"HTTP {e.code} {path}: {msg}"[:200])
        pages.append(d)
        token = d.get("nextPageToken")
        if not token:
            break
    return pages

def _q(x, *path):
    for k in path:
        x = (x or {}).get(k)
    return x

def _wmo(cond):
    """Google condition type -> the WMO code the page's icons and Thai labels use."""
    t = (cond or {}).get("type") or ""
    if "THUNDER" in t:
        return 99 if "HEAVY" in t else 95
    if "HAIL" in t:
        return 96
    if "SNOW" in t:
        return 71
    if "RAIN" in t or "SHOWER" in t:
        sh = "SHOWER" in t
        if "HEAVY" in t:
            return 82 if sh else 65
        if "LIGHT" in t or "CHANCE" in t or "SCATTERED" in t:
            return 80 if sh else 61
        return 81 if sh else 63
    return {"CLEAR": 0, "MOSTLY_CLEAR": 1, "PARTLY_CLOUDY": 2, "FOG": 45}.get(t, 3)

def google_weather(lat, lon):
    """Current conditions, 48 h hourly and 7-day forecast in the same shape as weather()."""
    local = lambda iso: dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    cur = _gw("currentConditions:lookup", lat, lon)[0]
    hourly = google_hours(lat, lon, 48)
    return {"current": {"time": local(cur["currentTime"]).strftime("%Y-%m-%dT%H:%M"),
                        "temp": _q(cur, "temperature", "degrees"), "rh": cur.get("relativeHumidity"),
                        "rain": _q(cur, "precipitation", "qpf", "quantity") or 0,
                        "code": _wmo(cur.get("weatherCondition")), "desc": _q(cur, "weatherCondition", "description", "text"),
                        "wind": _q(cur, "wind", "speed", "value")},
            "hourly": hourly, "daily": google_days(lat, lon)}

def google_hours(lat, lon, n):
    """Next n hours: rain (mm), chance of rain and of thunderstorms (%), gusts (km/h)."""
    local = lambda iso: dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    out = []
    for h in [h for p in _gw("forecast/hours:lookup", lat, lon, hours=n, pageSize=24) for h in p.get("forecastHours", [])][:n]:
        t = local(_q(h, "interval", "startTime"))
        out.append({"t": t.strftime("%H:%M"), "d": t.strftime("%Y-%m-%d"),
                    "p": _q(h, "precipitation", "qpf", "quantity") or 0,
                    "pp": _q(h, "precipitation", "probability", "percent") or 0,
                    "ts": h.get("thunderstormProbability"), "gust": _q(h, "wind", "gust", "value")})
    return out

def google_alerts(lat, lon):
    """Official alerts Google relays for a point (from national CAP feeds). Thailand is not covered yet:
    the 'not supported for this location' reply counts as no alerts, so they appear here once it is."""
    try:
        pages = _gw("publicAlerts:lookup", lat, lon)
    except RuntimeError as e:
        if "not supported" in str(e):
            return []
        raise
    out = []
    for a in [a for p in pages for a in p.get("weatherAlerts", [])]:
        ds = a.get("dataSource") or {}
        out.append({"title": _q(a, "alertTitle", "text"), "event": a.get("eventType"), "area": a.get("areaName"),
                    "severity": (a.get("severity") or "").replace("SEVERITY_", ""), "start": a.get("startTime"), "end": a.get("expirationTime"),
                    "source": ds.get("name") or ds.get("publisher"), "url": ds.get("authorityUri"), "text": (a.get("description") or "")[:600]})
    return out

def google_days(lat, lon):
    """10-day forecast (the API's maximum, one page) in the shape of weather()["daily"]: rain is day + night,
    pp/ts the higher of the two halves."""
    daily = []
    for x in [x for p in _gw("forecast/days:lookup", lat, lon, days=10, pageSize=10) for x in p.get("forecastDays", [])]:
        dd, parts = x["displayDate"], [x.get("daytimeForecast") or {}, x.get("nighttimeForecast") or {}]
        pps = [v for v in (_q(p, "precipitation", "probability", "percent") for p in parts) if v is not None]
        daily.append({"date": f"{dd['year']:04}-{dd['month']:02}-{dd['day']:02}", "code": _wmo(parts[0].get("weatherCondition")),
                      "tmax": _q(x, "maxTemperature", "degrees"), "tmin": _q(x, "minTemperature", "degrees"),
                      "rain": round(sum(_q(p, "precipitation", "qpf", "quantity") or 0 for p in parts), 1),
                      "pp": max(pps) if pps else None,
                      "ts": max([v for v in (p.get("thunderstormProbability") for p in parts) if v is not None], default=None)})
    return daily

def with_google(w, lat, lon, place):
    """Put Google's current conditions and forecast over an Open-Meteo block. Past days stay Open-Meteo
    (they feed the rain-so-far totals); on any Google failure the block is returned unchanged."""
    if not GW_KEY:
        return w
    try:
        g = google_weather(lat, lon)
    except Exception as e:
        GW_ERR[place] = f"{type(e).__name__}: {e}"[:200]
        print("WARN google weather", place, GW_ERR[place], file=sys.stderr)
        return w
    if not w:
        return dict(g, src="google")
    today = g["current"]["time"][:10]
    daily = [x for x in w.get("daily", []) if x["date"] < today] + g["daily"] if g["daily"] else w.get("daily", [])
    out = dict(w, current=g["current"], hourly=g["hourly"] or w.get("hourly", []), daily=daily, src="google")
    out.pop("stale", None)
    return out

# ---- Bangkok by area: six points spread over the city for the rain section (Google when keyed) ----
BKK_ZONES = [("ตอนกลาง", "พระนคร · ปทุมวัน · ดุสิต", 13.75, 100.51), ("ตอนเหนือ", "จตุจักร · บางเขน · ดอนเมือง", 13.86, 100.60),
             ("ตะวันออก", "บางกะปิ · มีนบุรี · ลาดกระบัง", 13.79, 100.74), ("ตอนใต้", "สาทร · คลองเตย · บางนา", 13.70, 100.58),
             ("ฝั่งธนฯ เหนือ", "บางกอกน้อย · ตลิ่งชัน · ทวีวัฒนา", 13.77, 100.43), ("ฝั่งธนฯ ใต้", "ธนบุรี · บางขุนเทียน · บางแค", 13.66, 100.44)]

def _zone_sum(hours, days):
    mx = lambda k: max([x[k] for x in hours if x.get(k) is not None], default=None)
    pk = max(hours, key=lambda x: x["p"] or 0) if hours else None
    return {"next3": round(sum(x["p"] or 0 for x in hours[:3]), 1), "next24": round(sum(x["p"] or 0 for x in hours), 1),
            "pp": mx("pp"), "ts": mx("ts"), "gust": mx("gust"),
            "peak": {"t": pk["t"], "d": pk["d"], "p": pk["p"]} if pk and (pk["p"] or 0) > 0 else None,
            "days": [{"date": x["date"], "rain": x["rain"], "pp": x["pp"]} for x in days][:3]}

def bkk_zones():
    """Next 24 h and the next 3 days for each area. Open-Meteo first (one call for all six), then Google per area."""
    q = urllib.parse.urlencode({"latitude": ",".join(str(z[2]) for z in BKK_ZONES), "longitude": ",".join(str(z[3]) for z in BKK_ZONES),
                                "timezone": "Asia/Bangkok", "hourly": "precipitation,precipitation_probability,wind_gusts_10m",
                                "daily": "precipitation_sum,precipitation_probability_max", "forecast_days": 3})
    res = json.loads(get("https://api.open-meteo.com/v1/forecast?" + q))
    res = res if isinstance(res, list) else [res]
    now = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H")
    out = []
    for (name, ex, lat, lon), d in zip(BKK_ZONES, res):
        h = d["hourly"]
        i = next((k for k, t in enumerate(h["time"]) if t[:13] >= now), 0)
        hours = [{"t": h["time"][k][11:16], "d": h["time"][k][:10], "p": h["precipitation"][k],
                  "pp": h["precipitation_probability"][k], "gust": h["wind_gusts_10m"][k]} for k in range(i, min(i + 24, len(h["time"])))]
        days = [{"date": t, "rain": r, "pp": pp} for t, r, pp in
                zip(d["daily"]["time"], d["daily"]["precipitation_sum"], d["daily"]["precipitation_probability_max"])]
        out.append(dict(name=name, ex=ex, p=[lat, lon], src="open-meteo", **_zone_sum(hours, days)))
    if GW_KEY:
        for z in out:
            try:
                z.update(_zone_sum(google_hours(z["p"][0], z["p"][1], 24), google_days(z["p"][0], z["p"][1])), src="google")
            except Exception as e:  # this area keeps Open-Meteo
                GW_ERR["bkk_zones"] = f"{type(e).__name__}: {e}"[:200]
    return out

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
    by_code, area, provs = {}, [], {k: [] for k in PROVS}
    names = {v["name"]: k for k, v in PROVS.items()}
    for r in rows:
        p = pack(r)
        if p["code"]:
            by_code[p["code"]] = p
        if p["prov"] in TW_PROV:
            area.append(p)
        if p["prov"] in names:
            provs[names[p["prov"]]].append(p)
    key = lambda x: x["pct"] if x["pct"] is not None else -1
    area.sort(key=key, reverse=True)
    for v in provs.values():
        v.sort(key=key, reverse=True)
    return dict({"river": [by_code[c] for c in TW_RIVER if c in by_code], "area": area}, **provs)

# ---- provinces outside Bangkok (จ.ระยอง, จ.เชียงใหม่): the same blocks for each ----
RAYONG_PTS = [("เมืองระยอง", 12.6814, 101.2816), ("บ้านค่าย", 12.7068, 101.3004), ("ปลวกแดง", 12.9833, 101.1667),
              ("วังจันทร์", 13.0333, 101.4), ("แกลง", 12.7833, 101.65)]
CM_PTS = [("เมืองเชียงใหม่", 18.7883, 98.9853), ("แม่ริม", 18.9136, 98.9444), ("สันกำแพง", 18.7456, 99.1203),
          ("จอมทอง", 18.4183, 98.6758), ("ฝาง", 19.9192, 99.2133)]
PROVS = {
    "rayong": {"name": "ระยอง", "pts": RAYONG_PTS,
               "rivers": [("แม่น้ำระยอง", 12.69, 101.27), ("แม่น้ำประแสร์", 12.72, 101.66)],
               "news": ("น้ำท่วม ระยอง", "ระยอง ฝนตกหนัก น้ำป่า อพยพ", "ระยอง ศูนย์พักพิง น้ำท่วม", "ระยอง ถนน น้ำท่วม เส้นทาง"),
               "radar": ("https://weather.tmd.go.th/ryg/rygloop.gif", "radar_ry", "https://weather.tmd.go.th/rygloop.php"),
               "bbox": (12.55, 13.30, 101.05, 101.95), "marine": True},
    "chiangmai": {"name": "เชียงใหม่", "pts": CM_PTS,
                  "rivers": [("แม่น้ำปิง (ตัวเมือง)", 18.79, 99.00), ("แม่น้ำปิง (แม่แตง)", 19.12, 98.94)],
                  "news": ("น้ำท่วม เชียงใหม่", "เชียงใหม่ ฝนตกหนัก น้ำป่า แม่น้ำปิง", "เชียงใหม่ ฝุ่น PM2.5 หมอกควัน", "เชียงใหม่ ถนน น้ำท่วม เส้นทาง"),
                  "radar": ("https://weather.tmd.go.th/cmp/cmpLoop.gif", "radar_cm", "https://weather.tmd.go.th/cmploop.php"),
                  "bbox": (17.20, 20.15, 98.05, 99.60), "marine": False},
}

def prov_weather(key):
    """Open-Meteo for five points across the province: now, rain in the last/next 24 h, 10-day forecast for the city."""
    P = PROVS[key]["pts"]
    q = urllib.parse.urlencode({
        "latitude": ",".join(str(p[1]) for p in P), "longitude": ",".join(str(p[2]) for p in P),
        "timezone": "Asia/Bangkok",
        "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "hourly": "precipitation,precipitation_probability",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
        "past_days": 3, "forecast_days": 10})
    res = json.loads(get("https://api.open-meteo.com/v1/forecast?" + q))
    res = res if isinstance(res, list) else [res]
    now = res[0]["current"]["time"][:13]
    pts = []
    for (name, lat, lon), d in zip(P, res):
        h = d["hourly"]
        i = next((k for k, t in enumerate(h["time"]) if t[:13] >= now), 0)
        rain = [x or 0 for x in h["precipitation"]]
        pts.append({"name": name, "p": [lat, lon],
                    "past24": round(sum(rain[max(0, i - 24):i]), 1), "next24": round(sum(rain[i:i + 24]), 1),
                    "next48": round(sum(rain[i:i + 48]), 1), "next72": round(sum(rain[i:i + 72]), 1),
                    "pp": max([x or 0 for x in h["precipitation_probability"][i:i + 24]] or [0]),
                    "now": d["current"]["precipitation"],
                    "days": [{"date": t, "rain": r, "pp": pp} for t, r, pp in
                             zip(d["daily"]["time"], d["daily"]["precipitation_sum"], d["daily"]["precipitation_probability_max"])
                             if t >= now[:10]][:10]})
    c, dd, h0 = res[0]["current"], res[0]["daily"], res[0]["hourly"]
    i0 = next((k for k, t in enumerate(h0["time"]) if t[:13] >= now), 0)
    daily = [{"date": dd["time"][k], "code": dd["weather_code"][k], "tmax": dd["temperature_2m_max"][k],
              "tmin": dd["temperature_2m_min"][k], "rain": dd["precipitation_sum"][k],
              "pp": dd["precipitation_probability_max"][k]} for k in range(len(dd["time"]))]
    hourly = [{"t": h0["time"][k][11:16], "d": h0["time"][k][:10], "p": h0["precipitation"][k], "pp": h0["precipitation_probability"][k]}
              for k in range(i0, min(i0 + 48, len(h0["time"])))]
    return {"current": {"time": c["time"], "temp": c["temperature_2m"], "rh": c["relative_humidity_2m"],
                        "rain": c["precipitation"], "code": c["weather_code"], "wind": c["wind_speed_10m"]},
            "points": pts, "daily": daily, "hourly": hourly}

def rayong_weather():
    return prov_weather("rayong")

MARINE_PT = (12.64, 101.28)  # sea off Rayong city (หาดแสงจันทร์/แม่รำพึง), where the Rayong river meets the sea

def rayong_marine():
    """Sea level incl. tide and wave height off Rayong city (Open-Meteo Marine, a model): 72 h hourly,
    plus each day's high waters and highest wave. High water at the river mouth slows drainage from the city."""
    q = urllib.parse.urlencode({"latitude": MARINE_PT[0], "longitude": MARINE_PT[1], "hourly": "wave_height,sea_level_height_msl",
                                "timezone": "Asia/Bangkok", "forecast_days": 7})
    h = json.loads(get("https://marine-api.open-meteo.com/v1/marine?" + q))["hourly"]
    t, sl, wv = h["time"], h["sea_level_height_msl"], h["wave_height"]
    now = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H")
    i0 = next((k for k, x in enumerate(t) if x[:13] >= now), 0)
    days = {}
    for k, x in enumerate(t):
        d = days.setdefault(x[:10], {"date": x[:10], "high": [], "wave": None})
        if wv[k] is not None:
            d["wave"] = max(d["wave"] or 0, round(wv[k], 2))
        if 0 < k < len(t) - 1 and None not in (sl[k - 1], sl[k], sl[k + 1]) and sl[k - 1] <= sl[k] > sl[k + 1]:
            d["high"].append({"t": x[11:16], "h": round(sl[k], 2)})
    return {"hourly": [{"t": t[k][11:16], "d": t[k][:10], "sl": _f(sl[k]), "wave": _f(wv[k])} for k in range(i0, min(i0 + 72, len(t)))],
            "days": [d for d in days.values() if d["date"] >= now[:10]][:7]}

def _glofas(lat, lon):
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": "river_discharge,river_discharge_max",
                                "past_days": 7, "forecast_days": 14})
    d = json.loads(get("https://flood-api.open-meteo.com/v1/flood?" + q))["daily"]
    return [{"date": d["time"][i], "q": _f(d["river_discharge"][i]), "qmax": _f(d["river_discharge_max"][i])} for i in range(len(d["time"]))]

def prov_rivers(key):
    """GloFAS discharge (model, ~5 km grid) for the province's main rivers: 7 days back, 14 ahead, with the worst-case member."""
    return [{"name": name, "series": _glofas(lat, lon)} for name, lat, lon in PROVS[key]["rivers"]]

TW_RAIN = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public/rain_24h"
TW_MAIN = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public/thailand_main"  # ~10 MB; only its "dam" block is used
_CACHE = {}

def _cached(key, fn):
    """One download per run for the big ThaiWater files that several blocks read."""
    if key not in _CACHE:
        _CACHE[key] = fn()
    return _CACHE[key]

def _prov(r):
    return ((r.get("geocode") or {}).get("province_name") or {}).get("th")

def prov_rain(key):
    """Measured rain over the last 24 h at every telemetry gauge in the province (ThaiWater), wettest first."""
    name, out = PROVS[key]["name"], []
    for r in _cached("rain", lambda: json.loads(get(TW_RAIN, timeout=90))["data"]):
        if _prov(r) != name or r.get("rain_24h") is None:
            continue
        s, g = r.get("station") or {}, r.get("geocode") or {}
        out.append({"name": (s.get("tele_station_name") or {}).get("th"), "amphoe": (g.get("amphoe_name") or {}).get("th"),
                    "agency": ((r.get("agency") or {}).get("agency_shortname") or {}).get("th"),
                    "p": [s.get("tele_station_lat"), s.get("tele_station_long")],
                    "mm": _f(r["rain_24h"]), "t": r.get("rainfall_datetime")})
    out.sort(key=lambda x: x["mm"] or 0, reverse=True)
    return out[:60]  # Chiang Mai alone has ~350 gauges; the wettest 60 tell the story

def _dam(r, river=None):
    d = r.get("dam") or {}
    return {"name": (d.get("dam_name") or {}).get("th"), "prov": _prov(r), "date": r.get("dam_date"), "river": river,
            "pct": _f(r.get("dam_storage_percent")), "storage": _f(r.get("dam_storage")),
            "normal": _f(d.get("normal_storage")), "max": _f(d.get("max_storage")),
            "inflow": _f(r.get("dam_inflow")), "release": _f(r.get("dam_released")), "spill": _f(r.get("dam_spilled"))}

def _dam_rows():
    return _cached("dams", lambda: json.loads(get(TW_MAIN, timeout=180))["dam"]["data"]["data"])

def prov_dams(key):
    """Large reservoirs in the province (RID daily report via ThaiWater). pct is storage against normal capacity,
    so it can pass 100; inflow/release/spill are million m³ per day."""
    out = [_dam(r) for r in _dam_rows() if _prov(r) == PROVS[key]["name"]]
    out.sort(key=lambda x: x["pct"] or 0, reverse=True)
    return out

# large dams of the Chao Phraya basin, upstream of Bangkok, north to south along the water's way
BKK_DAMS = [("ภูมิพล", "แม่น้ำปิง"), ("สิริกิติ์", "แม่น้ำน่าน"), ("แควน้อยบำรุงแดน", "แม่น้ำแควน้อย → น่าน"),
            ("ทับเสลา", "ห้วยทับเสลา → สะแกกรัง"), ("กระเสียว", "ลำกระเสียว → ท่าจีน"), ("ป่าสักชลสิทธิ์", "แม่น้ำป่าสัก")]

def bkk_dams():
    """The reservoirs whose releases reach Bangkok (Chao Phraya, Pasak and Tha Chin), in BKK_DAMS order."""
    rows = {((r.get("dam") or {}).get("dam_name") or {}).get("th"): r for r in _dam_rows()}
    return [_dam(rows[n], river) for n, river in BKK_DAMS if n in rows]

# rain over the catchments upstream of Bangkok, 10 days ahead: what will reach the dams and the river next
UPSTREAM_PTS = [("เหนือเขื่อนภูมิพล", "ลุ่มน้ำปิง · ตาก", 17.35, 98.85), ("เหนือเขื่อนสิริกิติ์", "ลุ่มน้ำน่าน · อุตรดิตถ์", 17.95, 100.65),
                ("นครสวรรค์ (ปากน้ำโพ)", "ปิง-น่านรวมเป็นเจ้าพระยา", 15.70, 100.13), ("เหนือเขื่อนป่าสักฯ", "ลุ่มน้ำป่าสัก · เพชรบูรณ์", 15.40, 101.10)]

def upstream():
    q = urllib.parse.urlencode({"latitude": ",".join(str(p[2]) for p in UPSTREAM_PTS), "longitude": ",".join(str(p[3]) for p in UPSTREAM_PTS),
                                "timezone": "Asia/Bangkok", "daily": "precipitation_sum,precipitation_probability_max", "forecast_days": 10})
    res = json.loads(get("https://api.open-meteo.com/v1/forecast?" + q))
    res = res if isinstance(res, list) else [res]
    out = []
    for (name, ex, lat, lon), d in zip(UPSTREAM_PTS, res):
        days = [{"date": t, "rain": r, "pp": pp} for t, r, pp in
                zip(d["daily"]["time"], d["daily"]["precipitation_sum"], d["daily"]["precipitation_probability_max"])]
        src = "open-meteo"
        if GW_KEY:
            try:
                days, src = [{"date": x["date"], "rain": x["rain"], "pp": x["pp"]} for x in google_days(lat, lon)], "google"
            except Exception as e:
                GW_ERR["upstream"] = f"{type(e).__name__}: {e}"[:200]
        out.append({"name": name, "ex": ex, "p": [lat, lon], "src": src, "days": days,
                    "sum10": round(sum(x["rain"] or 0 for x in days), 1)})
    return out

def prov_news(key):
    name, seen, out, ok = PROVS[key]["name"], set(), [], False
    for q in PROVS[key]["news"]:
        try:
            items = gnews(q, 30)
        except Exception as e:
            print("WARN", key, "news", q, e, file=sys.stderr); continue
        ok = True
        for it in items:
            k = re.sub(r"\W", "", it["title"])[:40]
            if name in it["title"] and k not in seen:
                seen.add(k); out.append(it)
    if not ok:
        raise RuntimeError(f"all {key} news queries failed")
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:25]

# ---- air quality: Pollution Control Department stations (Air4Thai, measured) + Open-Meteo CAMS forecast (model) ----
AQ_URL = "https://air4thai.pcd.go.th/services/getNewAQI_JSON.php"
AQ_AREAS = {"bkk": ("กรุงเทพ", 13.75, 100.50), "rayong": ("ระยอง", 12.68, 101.28), "chiangmai": ("เชียงใหม่", 18.79, 98.98)}

def _aqv(x):
    v = _f((x or {}).get("value"))
    return None if v is None or v < 0 else v

def _aq_ctx():
    """Normal certificate checks plus the intermediates Air4Thai fails to send (see certs/letsencrypt-yr.pem)."""
    ctx = ssl.create_default_context()
    pem = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "letsencrypt-yr.pem")
    if os.path.exists(pem):
        ctx.load_verify_locations(cafile=pem)
    return ctx

def air_quality():
    """Per area: measured values at each Air4Thai station (Thai AQI colour ids 1-5) and 72 h of modelled PM2.5/AQI.
    Each half fails on its own, so a down Air4Thai still leaves the forecast (and the other way round)."""
    out = {k: {"stations": None, "fc": None} for k in AQ_AREAS}
    try:
        for s in json.loads(get(AQ_URL, timeout=60, ctx=_aq_ctx()))["stations"]:
            a = s.get("AQILast") or {}
            for k, (needle, _, _) in AQ_AREAS.items():
                if needle not in (s.get("areaTH") or ""):
                    continue
                out[k]["stations"] = out[k]["stations"] or []
                aq = a.get("AQI") or {}
                out[k]["stations"].append({"id": s.get("stationID"), "name": (s.get("nameTH") or "").strip(), "area": s.get("areaTH"),
                    "p": [_f(s.get("lat")), _f(s.get("long"))], "t": f"{a.get('date', '')} {a.get('time', '')}".strip(),
                    "pm25": _aqv(a.get("PM25")), "pm10": _aqv(a.get("PM10")), "o3": _aqv(a.get("O3")), "co": _aqv(a.get("CO")),
                    "no2": _aqv(a.get("NO2")), "so2": _aqv(a.get("SO2")),
                    "aqi": (lambda v: None if v is None or v < 0 else int(v))(_f(aq.get("aqi"))),
                    "cid": (lambda c: c if 1 <= c <= 5 else 0)(int(_f(aq.get("color_id")) or 0)),  # 1 ดีมาก … 5 มีผลกระทบต่อสุขภาพ; 0 = no reading
                    "param": aq.get("param")})
        for k in out:
            if out[k]["stations"] is not None:
                out[k]["stations"].sort(key=lambda x: x["pm25"] if x["pm25"] is not None else -1, reverse=True)
    except Exception as e:
        print("WARN air4thai", e, file=sys.stderr)
    try:
        A = list(AQ_AREAS.items())
        q = urllib.parse.urlencode({"latitude": ",".join(str(v[1]) for _, v in A), "longitude": ",".join(str(v[2]) for _, v in A),
                                    "timezone": "Asia/Bangkok", "hourly": "pm2_5,pm10,us_aqi", "current": "pm2_5,pm10,us_aqi", "forecast_days": 4})
        res = json.loads(get("https://air-quality-api.open-meteo.com/v1/air-quality?" + q))
        res = res if isinstance(res, list) else [res]
        now = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H")
        for (k, _), d in zip(A, res):
            h = d["hourly"]
            i = next((j for j, t in enumerate(h["time"]) if t[:13] >= now), 0)
            out[k]["fc"] = {"now": {"pm25": _f(d["current"]["pm2_5"]), "pm10": _f(d["current"]["pm10"]), "aqi": d["current"]["us_aqi"]},
                            "hourly": [{"d": h["time"][j][:10], "t": h["time"][j][11:16], "pm25": _f(h["pm2_5"][j]), "aqi": h["us_aqi"][j]}
                                       for j in range(i, min(i + 72, len(h["time"])))]}
    except Exception as e:
        print("WARN aq forecast", e, file=sys.stderr)
    out["fetched"] = dt.datetime.now(TZ).isoformat(timespec="minutes")
    return out

# ---- BMA road-flood sensors (สำนักการระบายน้ำ กทม., weather.bangkok.go.th/flood/) ----
BMA_SENSORS = "https://weather.bangkok.go.th/Flood/PageMap/GetDataTable"
# flood_sub_status: 0 ขัดข้อง, 5 ขัดข้องชั่วคราว (both offline), 1 ปกติ, 2 น้ำท่วมเล็กน้อย, 3 น้ำท่วม

def _msdate(s):
    m = re.search(r"\d{10,}", s or "")
    return dt.datetime.fromtimestamp(int(m.group()) / 1000, TZ).isoformat(timespec="minutes") if m else None

BMA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
               "Accept": "application/json, text/javascript, */*; q=0.01", "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
               "X-Requested-With": "XMLHttpRequest", "Origin": "https://weather.bangkok.go.th",
               "Referer": "https://weather.bangkok.go.th/flood/", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
BMA_ERR = ""  # why the last bma_sensors() call failed (goes into feed.json: run logs are unreadable until a run ends)

def _bma_fetch():
    global BMA_ERR
    last = ""
    for k in range(3):
        try:
            req = urllib.request.Request(BMA_SENSORS, data=b"", headers=BMA_HEADERS, method="POST")  # POST only; GET is 404
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            try:
                rows = json.loads(body)
            except ValueError:
                last = f"HTTP {r.status} not JSON: {body[:120]!r}"
                break
            if not isinstance(rows, list):
                last = f"HTTP {r.status} JSON is {type(rows).__name__}: {str(rows)[:120]}"
                break
            BMA_ERR = ""
            return rows
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 403:
                why = "Cloudflare check" if b"Just a moment" in body else "access denied"
                last = f"HTTP 403 {why}: weather.bangkok.go.th refuses this machine"
                break
            last = f"HTTP {e.code} {e.reason}: {body[:120]!r}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:200]
        time.sleep(3 * (k + 1))
    BMA_ERR = last
    print("WARN bma_sensors", last, file=sys.stderr)
    raise RuntimeError(last)

BMA_FILE = os.environ.get("BMA_FILE")
BMA_FILE_MAX_H = 6

def bma_from_file():
    """The copy bma_push.py uploads from a PC the BMA site accepts; ignored once it is older than BMA_FILE_MAX_H."""
    if not BMA_FILE or not os.path.exists(BMA_FILE):
        return None
    try:
        b = json.load(open(BMA_FILE, encoding="utf-8"))
        age = dt.datetime.now(TZ) - dt.datetime.fromisoformat(b["fetched"])
        return dict(b, via="relay") if b.get("sites") and age < dt.timedelta(hours=BMA_FILE_MAX_H) else None
    except Exception as e:
        print("WARN bma file", e, file=sys.stderr)
        return None

def bma_sensors():
    global BMA_ERR
    try:
        return _bma_parse(_bma_fetch())
    except RuntimeError:
        raise
    except Exception as e:  # unexpected row shape: say so in the feed
        BMA_ERR = f"parse {type(e).__name__}: {e}"[:200]
        print("WARN bma_sensors", BMA_ERR, file=sys.stderr)
        raise

def _bma_parse(rows):
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
                    "since": _msdate(r.get("flood_start")) if (_f(r.get("flood")) or 0) > 0 else None})
    return {"fetched": dt.datetime.now(TZ).isoformat(timespec="minutes"),
            "source": "https://weather.bangkok.go.th/flood/", "sites": out}

# ---- flooded roads (BMA road-flood alerts as republished by Thairath) ----
import os, time
TR_RSS = "https://www.thairath.co.th/rss/news"
TR_SITEMAP = "https://www.thairath.co.th/sitemap-daily.xml"
ROAD_TITLE_NUM = re.compile(r"(เลี่ยง|ท่วม).*?\d+\s*(เส้นทาง|ถนน|สาย|จุด)|\d+\s*(เส้นทาง|ถนน|สาย)\s*.*ท่วม")
# un-numbered updates ("อัปเดตจุดน้ำท่วมขัง ถนนสายไหนควรเลี่ยง", "เส้นทางที่ยังมีน้ำท่วม") from outlets that quote the BMA list
ROAD_TITLE_PLAIN = re.compile(r"(อัปเดต|เช็ก|รู้ไว้|สรุป|เส้นทาง|ถนน).*(จุดน้ำท่วม|น้ำท่วมขัง|ยังมีน้ำท่วม|น้ำท่วม).*(เลี่ยง|ผ่านได้|ถนน|เส้นทาง)|(เลี่ยง|ผ่านได้).*(ถนน|เส้นทาง).*ท่วม")
OTHER_PROV = re.compile(r"ระยอง|จันทบุรี|ตราด|ชลบุรี|ปราจีน|ฉะเชิงเทรา|สมุทรสงคราม|นครปฐม|สุพรรณ|อยุธยา|เชียงใหม่|น่าน|ภูเก็ต|หาดใหญ่|สงขลา|ขอนแก่น|โคราช|นครราชสีมา")
def ROAD_TITLE_OK(t):
    return "ท่วม" in t and bool(ROAD_TITLE_NUM.search(t) or (ROAD_TITLE_PLAIN.search(t) and not OTHER_PROV.search(t)))
class _RT:  # keeps the old `ROAD_TITLE.search(title)` call sites working
    search = staticmethod(lambda t: ROAD_TITLE_OK(t) or None)
ROAD_TITLE = _RT()
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

_DEPTH = r"(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?\s*(?:ซม\.?|เซนติเมตร)"
_NAME_STOP = re.compile(r"แยก|ซอย|ช่วง|บริเวณ|ถึง|จาก|ตั้งแต่|หน้า|เลย|ตัด|ทั้งเส้น|ขาเข้า|ขาออก|ทั้งขา|มุ่งหน้า|ท่วม|น้ำ|รถ|งด|ระดับ")
_NOT_ROAD = {"สายหลัก", "สายรอง", "หลายสาย", "ทุกสาย", "บางสาย", "ที่", "ใน", "เส้นทาง", "หลัก", "รอง"}

def parse_roads_depth(body):
    """Fallback for un-numbered road-flood summaries: 'ถนนลาดพร้าว ซอยลาดพร้าว 113 - แยกบางกะปิ 20 ซม.',
    'ถนนศรีบูรพา ทั้งเส้น 30-40 ซม. งดสัญจรผ่าน'. A road is a 'ถนน…' / 'ถ.…' phrase with a depth in cm shortly after it."""
    t = re.sub(r"\s+", " ", body)
    t = re.sub(r"(แยก|ถึง|ตัดกับ|ตัด|จาก|เลย|กับ|ผ่าน|ขึ้น)\s*ถนน", r"\1", t)  # 'แยกถนนร่มเกล้า' is a junction, not a new road
    out = {}
    for m in re.finditer(r"(?:ถนน|ถ\.)\s*([ก-๙A-Za-z]{2,40}(?:\s[1-9](?=\s|$|\.))?)((?:(?!ถนน|ถ\.[ก-๙]).){0,110}?)" + _DEPTH, t):
        name, mid = m.group(1), m.group(2)
        k = _NAME_STOP.search(name)
        if k:  # 'ลาดกระบังแยกลาดกระบังร่มเกล้า…' (no spaces): the road ends where the junction starts
            mid, name = name[k.start():] + mid, name[:k.start()]
        name = name.strip()
        if len(name) < 3 or name in _NOT_ROAD:
            continue
        lo, hi = int(m.group(3)), int(m.group(4) or m.group(3))
        if not 1 <= max(lo, hi) <= 150:
            continue
        mid = re.sub(r"^\s*(บริเวณ|ช่วง)\s*", "", mid)
        mid = re.split(r"\s(?:มี|น้ำ|ท่วม|ระดับ|ประมาณ|สูง|รถ|งด|ยัง|เจ้าหน้าที่)", mid)[0].strip(" ,-–")  # drop trailing prose
        segs = []
        if mid and not mid.startswith("ทั้งเส้น"):
            m2 = re.match(r"(?:จาก\s*)?(.+?)\s*ถึง\s*(.+)$", mid)
            m3 = re.match(r"(.+?)\s+[-–]\s+(.+)$", mid)
            if m2:
                segs.append({"from": m2.group(1).strip(), "to": m2.group(2).strip()})
            elif m3 and len(m3.group(1)) < 40 and len(m3.group(2)) < 40:
                segs.append({"from": m3.group(1).strip(), "to": m3.group(2).strip()})
            elif len(mid) <= 60:
                segs.append({"near": mid})
        road = "ถ." + name
        if road in out:
            out[road]["depth"] = max(out[road]["depth"], hi)
            out[road]["segs"] += segs
        else:
            out[road] = {"road": road, "depth": hi, "segs": segs}
    return list(out.values())

GN_ROAD_QUERIES = ["กทม. เลี่ยง เส้นทาง น้ำท่วมขัง when:1d", "ถนน น้ำท่วมขัง กทม. เส้นทาง when:1d",
                   "อัปเดต จุดน้ำท่วมขัง ถนนไหน เลี่ยง กทม. when:1d", "กทม. ถนนยังมีน้ำท่วมขัง ผ่านได้ เลี่ยง when:1d"]

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

# ---- tidy road names / segments read from news text ----
BKK_DISTRICTS = ["พระนคร", "ดุสิต", "หนองจอก", "บางรัก", "บางเขน", "บางกะปิ", "ปทุมวัน", "ป้อมปราบศัตรูพ่าย", "พระโขนง", "มีนบุรี",
    "ลาดกระบัง", "ยานนาวา", "สัมพันธวงศ์", "พญาไท", "ธนบุรี", "บางกอกใหญ่", "ห้วยขวาง", "คลองสาน", "ตลิ่งชัน", "บางกอกน้อย", "บางขุนเทียน",
    "ภาษีเจริญ", "หนองแขม", "ราษฎร์บูรณะ", "บางพลัด", "ดินแดง", "บึงกุ่ม", "สาทร", "บางซื่อ", "จตุจักร", "บางคอแหลม", "ประเวศ", "คลองเตย",
    "สวนหลวง", "จอมทอง", "ดอนเมือง", "ราชเทวี", "ลาดพร้าว", "วัฒนา", "บางแค", "หลักสี่", "สายไหม", "คันนายาว", "สะพานสูง", "วังทองหลาง",
    "คลองสามวา", "บางนา", "ทวีวัฒนา", "ทุ่งครุ", "บางบอน"]
# words that end a road name in running text ("ถนนกรุงเทพกรีฑาตั้งแต่ซอย 9", "ถนนลาดกระบังตลอดสาย", "ถนนสูงประมาณ 30 ซม.")
# only words that cannot be part of a Bangkok road name; short ones ("ที่" in พระรามที่ 9, "งด" in ทางด่วน, "น้ำ" in ท่าน้ำนนท์) stay out
_NAME_CUT = re.compile(r"ตั้งแต่|ตลอด|ทั้งสาย|ทั้งเส้น|ทั้งขา|ทั้งสอง|แยก|ซอย|ช่วง|บริเวณ|ระหว่าง|ขาเข้า|ขาออก|มุ่งหน้า|ระดับ|ประมาณ|ท่วม|และ|หรือ|ตัดกับ|เนื่องจาก|ทำให้|แต่|โดย|ซึ่ง")
# a name that starts like this is prose, not a road ("ถนนสูงประมาณ 30 ซม.", "ถนนที่ยังมีน้ำ…")
_NAME_PROSE = re.compile(r"^(?:สูง|ประมาณ|น้ำ|ท่วม|ที่|และ|หรือ|ใน|ยัง|เป็น|รถ|งด|ระดับ|ตลอด|ทั้ง|แต่|โดย|ซึ่ง|มี|ซอย|แยก|ช่วง|บริเวณ)")
_PROSE_CUT = re.compile(r"\s*(?:ระดับน้ำ|สำหรับ|แต่|โดย|ซึ่ง|เนื่องจาก|ขณะที่|ทั้งนี้|อย่างไรก็ตาม|ส่วน|และยัง|ลดลง|เพิ่มขึ้น|ประมาณ|ท่วมสูง|สูง\s*\d|\d+\s*(?:ซม|เซนติเมตร))")
_SEG_JUNK = {"และ", "หรือ", "ที่", "ใน", "ของ", "บน", "ตลอดสาย", "ทั้งสาย", "ทั้งเส้น"}

def _tidy_name(name):
    """-> (road name without 'ถ.' / None, extra location text from the cut-off tail)."""
    n = re.sub(r"^(?:ถนน|ถ\.)\s*", "", name or "").strip(" ,.-–")
    extra = ""
    if _NAME_PROSE.match(n):
        return None, ""  # 'ถนนสูงประมาณ…', 'ถนนน้ำท่วม…': no road name, just prose
    m = _NAME_CUT.search(n)
    if m:
        if m.start() < 3:
            return None, ""
        n, extra = n[:m.start()], n[m.start():]
        em = re.match(r"(?:ตั้งแต่)?\s*(ซอย\s*\S+|แยก\s*\S+)", extra)
        extra = em.group(1) if em else ""
    for d in sorted(BKK_DISTRICTS, key=len, reverse=True):  # 'หลวงแพ่งลาดกระบัง' = road + district
        if n.endswith(d) and len(n) - len(d) >= 3 and n[-len(d) - 1] not in "-–":  # 'ประเวศ-ลาดกระบัง' is a route, keep it
            n, extra = n[:-len(d)], extra or "เขต" + d
            break
    n = re.sub(r"บาง$", "", n) if len(n) >= 6 else n  # 'หลวงแพ่งบาง': a cut-off place name, not part of the road
    n = n.strip(" ,.-–")
    return (n, extra) if len(n) >= 3 else (None, "")

def _tidy_text(t):
    t = _PROSE_CUT.split(re.sub(r"\s+", " ", t or ""))[0].strip(" ,.-–")
    t = re.sub(r"^(?:บริเวณ|ช่วง|ใกล้|หน้า)\s*", "", t).strip(" ,.-–") if len(t) > 12 else t
    return t[:60].rsplit(" ", 1)[0] if len(t) > 60 and " " in t[:60] else t[:60]

def _tidy_items(items):
    out = []
    for it in items:
        name, extra = _tidy_name(it["road"])
        if not name:
            continue
        segs = []
        for sg in it["segs"]:
            sg = dict(sg)
            for k in ("from", "to", "near"):
                if k in sg:
                    sg[k] = _tidy_text(sg[k])
            if "near" in sg and (len(sg["near"]) < 3 or sg["near"] in _SEG_JUNK):
                continue
            if "from" in sg and (len(sg["from"]) < 2 or len(sg.get("to", "")) < 2):
                continue
            segs.append(sg)
        if extra and not segs:
            segs.append({"near": extra})
        out.append(dict(it, road="ถ." + name, segs=segs))
    return out

_FALLBACK_USED = False  # set by _best_items: the loose depth-based reading produced the list

def _best_items(*texts):
    global _FALLBACK_USED
    _FALLBACK_USED = False
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
    if len(best) < 5:  # exact BMA-list formats found (almost) nothing: try the loose depth-based reading
        for t in texts:
            try:
                items = parse_roads_depth(t) if t else []
            except Exception:
                items = []
            if len(items) > len(best):
                best, _FALLBACK_USED = items, True
    best = _tidy_items(best)
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
                [(0, t, l, True) for _, t, l in _gn_road_candidates()[:10]]
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
        print(f"roads candidate: {len(items)} roads{' (loose)' if _FALLBACK_USED else ''} · {(headline or title)[:70]} · {link[:70]}", file=sys.stderr)
        if items:
            print("   " + " | ".join(f"{i['road']} {i['depth'] or '-'}cm" for i in items[:6]), file=sys.stderr)
        # water recedes -> lists get short; the loose reading is trusted from 3 roads, the exact formats from 5
        if len(items) < 3:
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

# ---- flood events (Longdo Event by iTIC Foundation: DOH admin + citizen reports, CC BY 4.0) ----
EVENTS_URL = "https://event.longdo.com/feed/json"
EVENTS_BBOX = (13.45, 14.10, 100.30, 101.00)  # lat min/max, lon min/max: Bangkok and the surrounding provinces
_longdo_rows = None

def flood_events(bbox=EVENTS_BBOX):
    global _longdo_rows
    if _longdo_rows is None:  # one download serves both areas
        _longdo_rows = json.loads(get(EVENTS_URL))
    now = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")  # the feed's times are Thai local time
    out = []
    for e in _longdo_rows:
        if str(e.get("type")) != "6" or (e.get("stop") or "") < now:  # type 6 = น้ำท่วม; skip expired
            continue
        lat, lon = float(e["latitude"]), float(e["longitude"])
        if not (bbox[0] <= lat <= bbox[1] and bbox[2] <= lon <= bbox[3]):
            continue
        who = (e.get("contributor") or "").lower()
        out.append({"id": str(e["eid"]), "t": clean(e["title"]), "d": clean(e.get("description") or "")[:200],
                    "p": [round(lat, 5), round(lon, 5)], "s": e["start"], "e": e["stop"],
                    "by": "doh" if who.startswith("doh") else "itic" if who.startswith("itic") else "user", "sev": e.get("severity")})
    out.sort(key=lambda x: x["s"], reverse=True)
    return {"fetched": dt.datetime.now(TZ).isoformat(timespec="minutes"), "src": "Longdo Event / iTIC Foundation (CC BY 4.0)",
            "source": "https://event.longdo.com/", "items": out[:200]}

# ---- rain radar (TMD Suvarnabhumi 120 km loop) ----
import base64, io
RADAR_GIF = "https://weather.tmd.go.th/svp/svp120loop.gif"
RADAR_FRAMES = 8

def radar(out_dir=None):
    return _radar(RADAR_GIF, out_dir or os.environ.get("RADAR_DIR", "radar"), "https://weather.tmd.go.th/svp120loop.php")

def prov_radar(key):
    """Frames go next to the Bangkok ones, as radar_ry/f<i>.json, radar_cm/f<i>.json."""
    gif, sub, page = PROVS[key]["radar"]
    base = os.environ.get("RADAR_DIR", "radar").rstrip("/\\")
    return _radar(gif, os.path.join(os.path.dirname(base) or ".", sub), page)

def _radar(gif, out_dir, source):
    """Writes the most recent loop frames as <out_dir>/f<i>.json ({i, n, img: data-URI webp, fetched})
    for the dashboard's db collection "radar". Returns metadata for the main feed doc."""
    from PIL import Image, ImageSequence
    im = Image.open(io.BytesIO(get(gif, timeout=60)))
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
    return {"frames": len(frames), "fetched": fetched, "source": source}

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
            "east": regions.get("ภาคตะวันออก", ""), "north": regions.get("ภาคเหนือ", "")}

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
        "zones": safe(bkk_zones, None),
        "dams": safe(bkk_dams, None),
        "upstream": safe(upstream, None),
        "aq": safe(air_quality, None),
        "bma": safe(bma_sensors, None),
        "events": safe(flood_events, None),
        "roads": safe(roads, None),
        "reports": safe(road_reports, None),
        "radar": safe(radar, None),
        "tmd": {"warnings": safe(tmd_warnings, None), "daily": safe(tmd_daily, None)},
        "news": news(),
    }
    if doc["bma"] is None:
        if BMA_ERR:
            doc["bma_err"] = BMA_ERR
        doc["bma"] = bma_from_file()
    for key in PROVS:
        doc[key] = {"weather": safe(lambda: prov_weather(key), None), "news": safe(lambda: prov_news(key), None),
                    "rain": safe(lambda: prov_rain(key), None), "dams": safe(lambda: prov_dams(key), None),
                    "rivers": safe(lambda: prov_rivers(key), None), "radar": safe(lambda: prov_radar(key), None),
                    "marine": safe(rayong_marine, None) if PROVS[key]["marine"] else None,
                    "events": safe(lambda: flood_events(PROVS[key]["bbox"]), None)}
    prev_path = os.environ.get("PREV_FEED")
    if prev_path and os.path.exists(prev_path):
        try:
            prev = json.load(open(prev_path, encoding="utf-8"))
            prev = prev.get("data", prev) if isinstance(prev, dict) else {}
            if doc["roads"] is None and prev.get("roads"):
                doc["roads"] = dict(prev["roads"], carried=True)
            # keep the last good block when a source fails this run
            for k in ("weather", "river", "thaiwater", "bma", "radar", "zones", "events", "aq"):
                if not doc.get(k) and prev.get(k):
                    doc[k] = dict(prev[k], stale=True) if isinstance(prev[k], dict) else prev[k]
            for k in ("dams", "upstream"):  # lists: items carry their own dates
                if doc[k] is None:
                    doc[k] = prev.get(k) or []
            if not (doc.get("tmd") or {}).get("daily") and (prev.get("tmd") or {}).get("daily"):
                doc["tmd"]["daily"] = prev["tmd"]["daily"]
            # None = fetch failed (an empty list is a real "nothing new" answer)
            if doc["tmd"]["warnings"] is None:
                doc["tmd"]["warnings"] = (prev.get("tmd") or {}).get("warnings") or []
            doc["reports"] = merge_reports(doc["reports"], prev.get("reports"))
            for key in PROVS:
                pr, cur = prev.get(key) or {}, doc[key]
                if cur["weather"] is None and pr.get("weather"):
                    cur["weather"] = dict(pr["weather"], stale=True)
                for k in ("news", "rain", "dams", "rivers"):  # each item carries its own time, so the page can tell it is old
                    if cur[k] is None:
                        cur[k] = pr.get(k) or []
                for k in ("marine", "radar", "events"):
                    if cur[k] is None and pr.get(k) and (k != "marine" or PROVS[key]["marine"]):
                        cur[k] = dict(pr[k], stale=True)
            for k, v in doc["news"].items():
                if v is None:
                    doc["news"][k] = (prev.get("news") or {}).get(k) or []
        except Exception as e:
            print("WARN prev feed", e, file=sys.stderr)
    if doc["tmd"]["warnings"] is None:
        doc["tmd"]["warnings"] = []
    # Google (when a key is set) goes over whatever Open-Meteo gave, fresh or carried over
    doc["weather"] = with_google(doc["weather"], LAT, LON, "bkk")
    for key in PROVS:
        p0 = PROVS[key]["pts"][0]
        rw = doc[key]["weather"] = with_google(doc[key]["weather"], p0[1], p0[2], key)
        if rw and rw.get("src") == "google" and rw.get("points"):  # the city row of the district table follows Google too
            h = rw["hourly"]
            rw["points"][0].update(next24=round(sum(x["p"] for x in h[:24]), 1), next48=round(sum(x["p"] for x in h[:48]), 1),
                                   pp=max([x["pp"] or 0 for x in h[:24]], default=0))
            try:
                gd = [google_days(p["p"][0], p["p"][1]) for p in rw["points"]]
                for p, days in zip(rw["points"], gd):
                    p["days"] = [{"date": x["date"], "rain": x["rain"], "pp": x["pp"]} for x in days]
                rw["points_src"] = "google"
            except Exception as e:
                GW_ERR[key + "_points"] = f"{type(e).__name__}: {e}"[:200]
    if GW_KEY:
        doc["galerts"] = {}
        places = [("bkk", LAT, LON)] + [(k, PROVS[k]["pts"][0][1], PROVS[k]["pts"][0][2]) for k in PROVS]
        for place, lat, lon in places:
            try:
                doc["galerts"][place] = google_alerts(lat, lon)
            except Exception as e:
                GW_ERR["alerts_" + place] = f"{type(e).__name__}: {e}"[:200]
    if GW_ERR:
        doc["google_err"] = GW_ERR
    if doc["reports"] is None:
        doc["reports"] = []
    tw = doc.get("thaiwater") or {}
    for key in PROVS:
        name, cur = PROVS[key]["name"], doc[key]
        cur.update({k: cur[k] or [] for k in ("news", "rain", "dams", "rivers")})
        cur.update({"warnings": [w for w in doc["tmd"]["warnings"] if name in (w.get("title") or "") + (w.get("headline") or "")],
                    "water": tw.get(key) or []})
    doc["news"] = {k: v or [] for k, v in doc["news"].items()}
    if _GEO_CACHE_PATH:
        json.dump(_geo_cache, open(_GEO_CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    out = sys.argv[1] if len(sys.argv) > 1 else "feed.json"
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    open(out, "w", encoding="utf-8").write(s)
    pv = lambda k: (f"{k}: weather={(doc[k]['weather'] or {}).get('src', 'none')} radar={(doc[k].get('radar') or {}).get('frames', 0)} "
                    f"water={len(doc[k]['water'])} rain={len(doc[k]['rain'])} dams={len(doc[k]['dams'])} rivers={len(doc[k]['rivers'])} "
                    f"news={len(doc[k]['news'])} events={len((doc[k].get('events') or {}).get('items', []))}")
    aq = doc.get("aq") or {}
    print(f"wrote {out}: {len(s.encode())} bytes; news " + ", ".join(f"{k}={len(v)}" for k, v in doc["news"].items()) +
          f"; google={'off' if not GW_KEY else ','.join(k + ('=FAIL' if k in GW_ERR else '=ok') for k in ['bkk', *PROVS])}"
          f"; dams_bkk={len(doc['dams'] or [])} upstream={len(doc['upstream'] or [])}"
          f"; aq=" + ",".join(f"{k}:{len((aq.get(k) or {}).get('stations') or [])}st{'+fc' if (aq.get(k) or {}).get('fc') else ''}" for k in AQ_AREAS) +
          f"; {'; '.join(pv(k) for k in PROVS)}; reports={len(doc['reports'])}; events={len((doc['events'] or {}).get('items', []))}"
          f"; bma_sites={len((doc['bma'] or {}).get('sites', []))}{' (relay)' if (doc['bma'] or {}).get('via') else ''}"
          f"; radar_frames={(doc['radar'] or {}).get('frames', 0)}; roads={len((doc['roads'] or {}).get('items', []))}{' (carried over)' if (doc['roads'] or {}).get('carried') else ''}")

if __name__ == "__main__":
    main()
