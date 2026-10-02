#!/usr/bin/env python3
"""Flood risk points of Bangkok (จุดเสี่ยงน้ำท่วม 737 จุด, สำนักการระบายน้ำ กทม. via bmagis.bangkok.go.th).
Read-only query of the public ArcGIS FeatureServer layer; writes a compact data/risk.json for the page to load on demand.
The points are structural (they change a few times a year), so the file is refreshed at most every REFRESH_DAYS days.
Usage: python3 fetch_risk.py [out.json] [--force]
"""
import json, os, sys, time, urllib.parse as up, urllib.request, datetime as dt

LAYER = "https://bmagis.bangkok.go.th/arcgis/rest/services/Hosted/" + up.quote("จุดเสี่ยงรวม_737") + "/FeatureServer/0/query"
SRC = "สำนักการระบายน้ำ กทม. (bmagis.bangkok.go.th)"
REFRESH_DAYS = 7
CAUSE = {"น้ำฝน สนข.": "rain_d", "น้ำฝน สนน.": "rain_dds", "น้ำหนุน": "tide"}  # สนข. = สำนักงานเขต, สนน. = สำนักการระบายน้ำ
TZ = dt.timezone(dt.timedelta(hours=7))


def fetch():
    q = up.urlencode({"where": "1=1", "outFields": "*", "outSR": 4326, "returnGeometry": "true", "f": "json"})
    last = None
    for k in range(3):
        try:
            req = urllib.request.Request(LAYER + "?" + q, headers={"User-Agent": "Mozilla/5.0 FloodFight69"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            if d.get("error"):
                raise RuntimeError(str(d["error"])[:200])
            return d
        except Exception as e:
            last = e
            time.sleep(3 * (k + 1))
    raise last


def build(d):
    feats = d["features"]
    if d.get("exceededTransferLimit") or len(feats) < 100:
        raise RuntimeError(f"unexpected layer size {len(feats)}")
    rows = []
    for f in feats:
        a, g = f["attributes"], f.get("geometry") or {}
        lat, lng = g.get("y", a.get("y")), g.get("x", a.get("x"))
        if lat is None or lng is None:
            continue
        name = (a["name"] or "").strip()
        name = name.split(".", 1)[1].strip() if name.split(".", 1)[0].isdigit() and "." in name else name  # drop the "12." list number
        st = a["status_num"] or 0
        rows.append([round(lat, 5), round(lng, 5), name, (a["district"] or "").strip(), st,
                     CAUSE.get((a["problems"] or "").strip(), "other"), (a["project_name"] or "").strip()[:140]])
    return {"v": 1, "got": dt.datetime.now(TZ).isoformat(timespec="minutes"), "src": SRC, "n": len(rows),
            "status": {"1": "มีมาตรการเร่งด่วน", "2": "พื้นที่เอกชนหรือหน่วยงานราชการ", "3": "อยู่ระหว่างดำเนินการแก้ไข",
                       "4": "แก้ไขแล้วเสร็จบางส่วน", "5": "แก้ไขแล้วเสร็จ"},
            "cause": {"rain_d": "น้ำฝน (สำนักงานเขตดูแล)", "rain_dds": "น้ำฝน (สำนักการระบายน้ำดูแล)", "tide": "น้ำหนุน"},
            "cols": ["lat", "lng", "name", "district", "status", "cause", "project"], "p": rows}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else "risk.json"
    if "--force" not in sys.argv and os.path.exists(out) and time.time() - os.path.getmtime(out) < REFRESH_DAYS * 86400:
        print(f"{out} is fresh, skipping")
        return
    doc = build(fetch())
    open(out, "w", encoding="utf-8").write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {out}: {doc['n']} points")


if __name__ == "__main__":
    main()
