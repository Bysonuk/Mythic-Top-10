#!/usr/bin/env python3
"""
Upload the built addon zip to CurseForge.

Used by the GitHub workflow. Skips the upload when the addon hasn't changed,
or when one has already gone up today, so CurseForge gets at most one file a day.

Needs:
  CF_API_TOKEN   an API token from https://legacy.curseforge.com/account/api-tokens
  CF_PROJECT_ID  the numeric project ID shown on your CurseForge project page

Usage:
  python cf_upload.py site/MythicStats.zip --interface 120105 --marker .cf_last_upload
  python cf_upload.py site/MythicStats.zip --force          # ignore the once-a-day rule
"""

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date

API = "https://wow.curseforge.com/api"


def call(path, token, data=None, headers=None):
    req = urllib.request.Request(API + path, data=data,
                                 headers={"X-Api-Token": token, **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return json.loads(body) if body else {}


def version_name(interface):
    i = int(str(interface).strip())
    return f"{i // 10000}.{i // 100 % 100}.{i % 100}"


def game_version_ids(token, interfaces):
    """Turn Interface numbers like 120105 into CurseForge game version IDs."""
    versions = call("/game/versions", token)
    ids, names = [], []
    for iface in str(interfaces).split(","):
        want = version_name(iface)
        match = next((v for v in versions if v.get("name") == want), None)
        if not match:
            prefix = want.rsplit(".", 1)[0] + "."
            same = [v for v in versions if str(v.get("name", "")).startswith(prefix)]
            match = max(same, key=lambda v: v["id"]) if same else None
        if match and match["id"] not in ids:
            ids.append(match["id"])
            names.append(match["name"])
    if not ids:
        v = max(versions, key=lambda v: v["id"])
        ids, names = [v["id"]], [v["name"]]
    return ids, names


def multipart(fields, filename, filedata):
    boundary = uuid.uuid4().hex
    out = b""
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{os.path.basename(filename)}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    out += filedata + f"\r\n--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--interface", default="120105",
                    help="Interface number(s) the file supports, comma separated")
    ap.add_argument("--marker", default=".cf_last_upload")
    ap.add_argument("--display-name", default=None)
    ap.add_argument("--release-type", default="release", choices=["release", "beta", "alpha"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("CF_API_TOKEN")
    project = os.environ.get("CF_PROJECT_ID")
    if not token or not project:
        print("No CurseForge token or project ID set, so nothing was uploaded.")
        return 0
    if not os.path.exists(args.zip_path):
        print(f"{args.zip_path} doesn't exist, so nothing was uploaded.")
        return 0

    with open(args.zip_path, "rb") as f:
        blob = f.read()
    digest = hashlib.sha1(blob).hexdigest()
    today = date.today().isoformat()

    last = {}
    if os.path.exists(args.marker):
        try:
            with open(args.marker, encoding="utf-8") as f:
                last = json.load(f)
        except ValueError:
            last = {}
    if not args.force:
        if last.get("date") == today:
            print("Already uploaded to CurseForge today.")
            return 0
        if last.get("sha1") == digest:
            print("The addon hasn't changed since the last upload.")
            return 0

    try:
        gv_ids, gv_names = game_version_ids(token, args.interface)
    except urllib.error.HTTPError as e:
        print(f"Couldn't read CurseForge game versions ({e.code}). Nothing was uploaded.")
        return 0

    name = args.display_name or f"Mythic Stat Sheet {today}"
    meta = {
        "changelog": f"Rankings refreshed on {today}.",
        "changelogType": "text",
        "displayName": name,
        "gameVersions": gv_ids,
        "releaseType": args.release_type,
    }
    body, ctype = multipart({"metadata": json.dumps(meta)}, args.zip_path, blob)
    try:
        res = call(f"/projects/{project}/upload-file", token, body, {"Content-Type": ctype})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        print(f"CurseForge refused the upload ({e.code}): {detail}")
        return 0   # don't fail the whole workflow over this
    except urllib.error.URLError as e:
        print(f"Couldn't reach CurseForge ({e}). Nothing was uploaded.")
        return 0

    print(f"Uploaded {name} to CurseForge for {', '.join(gv_names)} (file {res.get('id')}).")
    with open(args.marker, "w", encoding="utf-8") as f:
        json.dump({"date": today, "sha1": digest, "file": res.get("id")}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
