#!/usr/bin/env python3
"""Publish a staged package: upload its blobs, then record it in the catalogue.

    python tools/publish_package.py <package.json> [--dry-run]

<package.json> is produced in the engine repository by
    python Utilities/PackageInspector.py <package-dir> --json out.json

Both halves are idempotent and safe to re-run:

  * blobs are content-addressed, so an upload is skipped when the asset already
    exists - identical hash means identical bytes
  * the Version row is checked first and a second publish of the same
    id@version is a hard error, because Baserow has no unique constraint and
    republishing would break every lock file that already trusts it

Rows are always written with Status = Pending. Approval is a human action in
Baserow, and no token here is allowed to update Status.

Environment:
    BASEROW_URL     base URL of the catalogue instance
    BASEROW_TOKEN   database token with create+read on Versions and Components
    GITHUB_TOKEN    token with contents:write on the artifact repo (for gh)
"""

import argparse, io, json, os, subprocess, sys, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_config():
    """registry.config.json, then registry.local.json, then the environment."""
    with io.open(os.path.join(ROOT, "registry.config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)

    local = os.path.join(ROOT, "registry.local.json")
    if os.path.isfile(local):
        with io.open(local, encoding="utf-8") as fh:
            for k, v in json.load(fh).get("baserow", {}).items():
                if k == "tables":
                    cfg["baserow"]["tables"].update(v)
                else:
                    cfg["baserow"][k] = v

    env = os.environ.get("BASEROW_URL", "").strip()
    if env:
        cfg["baserow"]["baseUrl"] = env
    return cfg


def catalogue_url(cfg):
    url = (cfg["baserow"].get("baseUrl") or "").strip()
    if not url:
        sys.exit("catalogue host is not set - export BASEROW_URL")
    return url.rstrip("/")


def run(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit("command failed: %s\n%s%s" % (" ".join(cmd), p.stdout, p.stderr))
    return p


# ------------------------------------------------------------------ blobs
def release_exists(repo, tag):
    return run(["gh", "release", "view", tag, "--repo", repo], check=False).returncode == 0


def asset_exists(repo, tag, name):
    p = run(["gh", "release", "view", tag, "--repo", repo, "--json", "assets"], check=False)
    if p.returncode != 0:
        return False
    try:
        return any(a.get("name") == name for a in json.loads(p.stdout).get("assets", []))
    except ValueError:
        return False


def upload_blobs(cfg, doc, pkg_dir, dry):
    repo = cfg["artifactRepo"]
    uploaded, skipped = [], []
    for c in doc["components"]:
        if not c.get("sha256"):
            continue
        tag, name = c["shard"], c["sha256"] + ".plpkg"
        local = os.path.join(pkg_dir, name)
        if not os.path.isfile(local):
            sys.exit("blob missing on disk: %s\n(re-run the packaging step so the .plpkg files exist)" % local)
        if dry:
            print("  would upload %-14s %s -> %s" % (c["name"], name[:16] + "...", tag))
            continue
        if not release_exists(repo, tag):
            run(["gh", "release", "create", tag, "--repo", repo, "--title", tag,
                 "--notes", "Content-addressed blob shard %s. Assets are named by SHA-256." % tag])
            print("  created release %s" % tag)
        if asset_exists(repo, tag, name):
            skipped.append(c["name"])
            print("  skip   %-14s already present in %s" % (c["name"], tag))
            continue
        run(["gh", "release", "upload", tag, local, "--repo", repo])
        uploaded.append(c["name"])
        print("  upload %-14s %s -> %s" % (c["name"], name[:16] + "...", tag))
    return uploaded, skipped


# ------------------------------------------------------------------ baserow
def api(cfg, method, path, token, body=None):
    url = "%s/api/%s" % (catalogue_url(cfg), path.lstrip("/"))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Token %s" % token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as ex:
        sys.exit("Baserow %s %s -> %s\n%s" % (method, url, ex.code, ex.read().decode("utf-8", "replace")))


def find_row(cfg, table, token, field, value):
    q = "database/rows/table/%d/?user_field_names=true&size=200" % table
    payload = api(cfg, "GET", q, token)
    for row in payload.get("results", []):
        if row.get(field) == value:
            return row
    return None


def write_rows(cfg, doc, token, dry):
    t = cfg["baserow"]["tables"]
    key, b = doc["key"], doc["baserow"]
    missing = [k for k in ("packages", "versions", "components") if not t.get(k)]
    configured = not missing

    # A dry run has to work before anything is configured - that is what it is for.
    if dry and (not configured or not token):
        why = ("table ids not set: " + ", ".join(missing)) if missing else "BASEROW_TOKEN is not set"
        print("  (%s, so nothing was checked against the live database)" % why)
        print("  would create Versions row   %s (Status=Pending)" % key)
        for c in b["components"]:
            print("  would create Components row %s  %s" % (c["Sha256"][:16] + "...", c["Name"]))
        return

    if not configured:
        sys.exit("these table ids are still 0 in registry.config.json: %s" % ", ".join(missing))

    existing = find_row(cfg, t["versions"], token, "Key", key)
    if existing:
        sys.exit("%s is already published.\nVersions are immutable - publish %s instead."
                 % (key, bump_hint(doc)))

    # Link fields resolve by the target row's primary-field text, so both of these
    # must already exist. Checking up front turns a mid-POST 400 into a clear message.
    pid = b["packages"]["Id"]
    if not find_row(cfg, t["packages"], token, "Id", pid):
        print("  no Packages row with Id '%s' - create it first" % pid)
        if not dry:
            sys.exit(1)

    pub = b["versions"].get("PublishedBy ->") or b["packages"].get("Publisher ->")
    if pub and t.get("publishers") and not find_row(cfg, t["publishers"], token, "Name", pub):
        print("  no Publishers row named '%s' - create it, or change Publisher in the manifest" % pub)
        if not dry:
            sys.exit(1)

    ver = dict(b["versions"])
    ver["Package"] = [ver.pop("Package ->")]
    ver["PublishedBy"] = [ver.pop("PublishedBy ->")]
    ver["Status"] = "Pending"

    if dry:
        print("  would create Versions row   %s (Status=Pending)" % key)
        for c in b["components"]:
            print("  would create Components row %s  %s" % (c["Sha256"][:16] + "...", c["Name"]))
        return

    api(cfg, "POST", "database/rows/table/%d/?user_field_names=true" % t["versions"], token, ver)
    print("  created Versions row   %s (Status=Pending)" % key)
    for c in b["components"]:
        row = dict(c)
        row["Version"] = [key]
        api(cfg, "POST", "database/rows/table/%d/?user_field_names=true" % t["components"], token, row)
    print("  created %d Components rows" % len(b["components"]))


def bump_hint(doc):
    try:
        a, b_, c = (doc["package"]["Version"] or "0.0.0").split(".")
        return "%s.%s.%d" % (a, b_, int(c) + 1)
    except Exception:
        return "a new version"


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Publish a staged package.")
    ap.add_argument("json", help="PackageInspector --json output")
    ap.add_argument("--blobs", metavar="DIR",
                    help="directory holding the <sha256>.plpkg files "
                         "(default: $PLASMA_BLOB_DIR)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    ap.add_argument("--skip-baserow", action="store_true", help="upload blobs only")
    ap.add_argument("--skip-blobs", action="store_true",
                    help="write catalogue rows only; useful before gh is authenticated")
    args = ap.parse_args()

    cfg = load_config()
    with io.open(args.json, encoding="utf-8") as fh:
        doc = json.load(fh)

    if not doc.get("valid"):
        fails = [v["check"] for v in doc.get("validation", []) if v["result"] == "FAIL"]
        sys.exit("package failed validation, refusing to publish:\n  " + "\n  ".join(fails))

    print("%s  (%d blobs)" % (doc["key"], sum(1 for c in doc["components"] if c.get("sha256"))))

    if args.skip_blobs:
        print("\nskipping blob upload")
    else:
        blob_dir = args.blobs or os.environ.get("PLASMA_BLOB_DIR", "").strip()
        if not blob_dir:
            sys.exit("where are the blobs? pass --blobs DIR or export PLASMA_BLOB_DIR "
                     "(the packaging step writes them next to the staged package).\n"
                     "To write catalogue rows without uploading anything, pass --skip-blobs.")
        print("\nblobs -> %s" % cfg["artifactRepo"])
        upload_blobs(cfg, doc, os.path.abspath(blob_dir), args.dry_run)

    if args.skip_baserow:
        print("\nskipping Baserow")
        return 0

    token = os.environ.get("BASEROW_TOKEN", "").strip()
    if not token and not args.dry_run:
        sys.exit("BASEROW_TOKEN is not set")
    print("\nrows -> %s" % cfg["baserow"]["baseUrl"])
    write_rows(cfg, doc, token, args.dry_run)

    print("\ndone. The version is Pending - approve it in the catalogue to publish it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
