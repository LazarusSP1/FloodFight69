#!/usr/bin/env python3
"""Relay for the BMA road-flood sensors: weather.bangkok.go.th puts GitHub's servers behind a Cloudflare check,
so this runs on a PC the site accepts, fetches the sensors, and uploads data/bma.json through the GitHub API
(needs the gh CLI, logged in). The hourly bot merges the file into feed.json while it is under 6 hours old.
Usage: python bma_push.py [--dry-run]
"""
import base64, json, subprocess, sys
from fetch_feed import bma_sensors

REPO, PATH = "LazarusSP1/FloodFight69", "data/bma.json"

def gh(*args, stdin=None):
    return subprocess.run(["gh", "api", *args], input=stdin, capture_output=True, text=True, encoding="utf-8", check=True).stdout

def main():
    b = bma_sensors()
    body = json.dumps(b, ensure_ascii=False, separators=(",", ":"))
    wet = sum(1 for s in b["sites"] if (s["cm"] or 0) > 0)
    if "--dry-run" in sys.argv:
        print(f"fetched {b['fetched']}: {len(b['sites'])} sites, {wet} with water; not uploaded")
        return
    try:
        sha = json.loads(gh(f"repos/{REPO}/contents/{PATH}"))["sha"]
    except subprocess.CalledProcessError:
        sha = None  # first upload
    msg = {"message": f"data: BMA sensors {b['fetched']}", "branch": "main",
           "content": base64.b64encode(body.encode("utf-8")).decode()}
    if sha:
        msg["sha"] = sha
    gh("-X", "PUT", f"repos/{REPO}/contents/{PATH}", "--input", "-", stdin=json.dumps(msg))
    print(f"uploaded {PATH} {b['fetched']}: {len(b['sites'])} sites, {wet} with water")

if __name__ == "__main__":
    main()
