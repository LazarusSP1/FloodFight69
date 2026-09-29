#!/usr/bin/env python3
"""Relay for the BMA road-flood sensors: weather.bangkok.go.th puts GitHub's servers behind a Cloudflare check,
so this runs on a machine the site accepts, fetches the sensors, and uploads data/bma.json through the GitHub API.
The hourly bot merges the file into feed.json while it is under 6 hours old.
Auth: GH_TOKEN (fine-grained token, this repo only, Contents read & write) — the Docker relay, see compose.yml;
without it the logged-in gh CLI is used.
Usage: python bma_push.py [--dry-run]
"""
import base64, json, os, subprocess, sys, urllib.request, urllib.error
from fetch_feed import bma_sensors

REPO, PATH = "LazarusSP1/FloodFight69", "data/bma.json"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

class NotFound(Exception):
    pass

def api(method, path, body=None):
    if TOKEN:
        req = urllib.request.Request("https://api.github.com/" + path, method=method,
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "FloodFight69-bma-relay"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(path)
            raise RuntimeError(f"GitHub API {method} {path}: HTTP {e.code} {e.read()[:200]!r}")
    p = subprocess.run(["gh", "api", "-X", method, path] + (["--input", "-"] if body else []),
                       input=json.dumps(body) if body else None, capture_output=True, text=True, encoding="utf-8")
    if p.returncode:
        if "404" in p.stderr + p.stdout:
            raise NotFound(path)
        raise RuntimeError(f"gh api {method} {path}: {p.stderr.strip()[:200]}")
    return json.loads(p.stdout)

def main():
    b = bma_sensors()
    body = json.dumps(b, ensure_ascii=False, separators=(",", ":"))
    wet = sum(1 for s in b["sites"] if (s["cm"] or 0) > 0)
    if "--dry-run" in sys.argv:
        print(f"fetched {b['fetched']}: {len(b['sites'])} sites, {wet} with water; not uploaded")
        return
    try:
        sha = api("GET", f"repos/{REPO}/contents/{PATH}")["sha"]
    except NotFound:
        sha = None  # first upload
    msg = {"message": f"data: BMA sensors {b['fetched']}", "branch": "main",
           "content": base64.b64encode(body.encode("utf-8")).decode()}
    if sha:
        msg["sha"] = sha
    api("PUT", f"repos/{REPO}/contents/{PATH}", msg)
    print(f"uploaded {PATH} {b['fetched']}: {len(b['sites'])} sites, {wet} with water")

if __name__ == "__main__":
    main()
