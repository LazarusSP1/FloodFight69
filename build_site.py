#!/usr/bin/env python3
"""Builds index.html for static hosting from template.html + data/*.json."""
import glob, json, os
HEAD = ('<!doctype html><html lang="th"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>'
        '<meta name="description" content="Dashboard ติดตามฝน น้ำท่วม ระดับน้ำ และพยากรณ์อากาศ กรุงเทพฯ 2569">'
        '</head><body>')
t = open("template.html", encoding="utf-8").read()
esc = lambda s: s.replace("</", "<\\/")
feed = open("data/feed.json", encoding="utf-8").read()
last = lambda d: (lambda fs: json.dumps({k: v for k, v in json.load(open(fs[-1])).items() if k in ("img", "fetched")}) if fs else "null")(
    sorted(glob.glob(d + "/f*.json"), key=lambda p: int(os.path.basename(p)[1:-5])))
radar, radar_ry = last("data/radar"), last("data/radar_ry")
alerts = open("data/alerts.json", encoding="utf-8").read() if os.path.exists("data/alerts.json") else "[]"
html = t.replace("__SEED__", esc(feed)).replace("__RADAR__", esc(radar)).replace("__RADAR_RY__", esc(radar_ry)).replace("__ALERTS__", esc(alerts))
open("index.html", "w", encoding="utf-8").write(HEAD + html + "</body></html>")
print("index.html", len(html))
