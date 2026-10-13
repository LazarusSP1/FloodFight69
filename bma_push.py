#!/usr/bin/env python3
"""Relay for the BMA road-flood sensors: weather.bangkok.go.th puts GitHub's servers behind a Cloudflare check,
so this runs on a machine the site accepts, fetches the sensors, and uploads data/bma.json through the GitHub API.
The hourly bot merges the file into feed.json while it is under 6 hours old.
Auth: GH_TOKEN (fine-grained token, this repo only, Contents read & write) — the Docker relay, see compose.yml;
without it the logged-in gh CLI is used.
Usage: python bma_push.py [--dry-run] [--loop [SECONDS]]
"""
import base64, json, os, subprocess, sys, time, urllib.request, urllib.error
from fetch_feed import bma_sensors

REPO, PATH = "LazarusSP1/FloodFight69", "data/bma.json"
_raw_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
# Ignore placeholder token from .env.example so gh CLI fallback works
TOKEN = _raw_token if _raw_token and not _raw_token.startswith("github_pat_xxx") else None

class NotFound(Exception):
    pass

def api(method, path, body=None):
    if TOKEN:
        headers = {
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "FloodFight69-bma-relay",
        }
        if body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request("https://api.github.com/" + path, method=method,
                                     data=json.dumps(body).encode() if body else None,
                                     headers=headers)
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

def push_once(dry_run=False):
    b = bma_sensors()
    body = json.dumps(b, ensure_ascii=False, separators=(",", ":"))
    wet = sum(1 for s in b["sites"] if (s["cm"] or 0) > 0)

    # Save to local file so non-Docker and local development always have up-to-date data
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PATH)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(body)
    except Exception as e:
        print(f"WARN saving {local_path}: {e}", file=sys.stderr)

    if dry_run:
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

def main():
    dry_run = "--dry-run" in sys.argv
    loop_arg = [a for a in sys.argv if a.startswith("--loop")]
    if not loop_arg:
        push_once(dry_run=dry_run)
        return

    interval = 3600
    for idx, arg in enumerate(sys.argv):
        if arg == "--loop" and idx + 1 < len(sys.argv) and sys.argv[idx + 1].isdigit():
            interval = int(sys.argv[idx + 1])
        elif arg.startswith("--loop="):
            val = arg.split("=", 1)[1]
            if val.isdigit():
                interval = int(val)

    print(f"Starting BMA relay loop (interval: {interval}s)...")
    while True:
        try:
            push_once(dry_run=dry_run)
        except Exception as e:
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            print(f"{now_iso} bma_push failed ({e}), retrying in {interval}s", file=sys.stderr)
        time.sleep(interval)

if __name__ == "__main__":
    main()
