#!/usr/bin/env python3
"""Render the published package index.

    python tools/render_index.py --out site/index.plPackageIndex

Sources, in order of preference:

  --source baserow   query Baserow for every Approved version (needs BASEROW_TOKEN)
  --source seed      build from the JSON files in seed/ (no network, no token)
  --source auto      baserow if a token and table ids are configured, else seed

The output is deterministic: packages, versions and components are all sorted, so
an unchanged catalogue renders byte-identical and CDN caches stay warm. The only
varying field is Generated, which is omitted unless --stamp is passed.
"""

import argparse, glob, io, json, os, sys, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_config():
    """registry.config.json, then registry.local.json, then the environment."""
    with io.open(os.path.join(ROOT, "registry.config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)

    # The catalogue host stays out of this public repo, so it arrives at run time -
    # from an untracked local file when developing, or BASEROW_URL in CI.
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
        sys.exit("catalogue host is not set - export BASEROW_URL (or set it as a repository variable)")
    return url.rstrip("/")


REQUIRED_TABLES = ("packages", "versions", "components")


def missing_tables(cfg, required=REQUIRED_TABLES):
    """Names of tables whose id is still 0, so messages can say which."""
    t = cfg["baserow"].get("tables", {})
    return [k for k in required if not t.get(k)]


def describe_tables(cfg):
    t = cfg["baserow"].get("tables", {})
    return ", ".join("%s=%s" % (k, t.get(k) or "unset")
                     for k in ("publishers", "packages", "versions", "components"))


# ------------------------------------------------------------------ baserow
def baserow_rows(cfg, table_id, token):
    """Fetch every row of a table, following pagination."""
    base = catalogue_url(cfg)
    url = "%s/api/database/rows/table/%d/?user_field_names=true&size=200" % (base, table_id)
    out = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": "Token %s" % token})
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
        out.extend(payload.get("results", []))
        url = payload.get("next")
    return out


def link_text(cell):
    """Baserow link cells arrive as [{id, value}] - we want the primary-field text."""
    if isinstance(cell, list):
        return [x.get("value") for x in cell if isinstance(x, dict) and x.get("value")]
    return []


def select_text(cell):
    if isinstance(cell, dict):
        return cell.get("value") or ""
    return cell or ""


def from_baserow(cfg, token):
    t = cfg["baserow"]["tables"]
    packages = {r["Id"]: r for r in baserow_rows(cfg, t["packages"], token) if r.get("Id")}
    versions = baserow_rows(cfg, t["versions"], token)
    components = baserow_rows(cfg, t["components"], token)

    by_version = {}
    for c in components:
        for key in link_text(c.get("Version")):
            by_version.setdefault(key, []).append(c)

    entries, revoked = {}, []
    for v in versions:
        status = select_text(v.get("Status"))
        key = v.get("Key") or ""
        if status == "Yanked":
            revoked.append((key, (v.get("YankReason") or "").strip()))
            continue
        if status != "Approved":
            continue
        if select_text(v.get("Channel")) not in cfg["channels"]:
            continue
        pkg_ids = link_text(v.get("Package"))
        if not pkg_ids or pkg_ids[0] not in packages:
            sys.stderr.write("skip %s: package row missing\n" % key)
            continue
        p = packages[pkg_ids[0]]
        if select_text(p.get("Visibility")) not in ("", "Public"):
            continue
        comps = []
        for c in sorted(by_version.get(key, []), key=lambda x: x.get("Sha256") or ""):
            comps.append({
                "name": select_text(c.get("Name")), "sha256": c.get("Sha256"),
                "bytes": int(c.get("Bytes") or 0), "optional": bool(c.get("Optional")),
                "label": c.get("Label") or None,
            })
        entries.setdefault(p["Id"], {"package": {
            "id": p["Id"], "displayName": p.get("DisplayName") or "",
            "category": select_text(p.get("Category")), "publisher": (link_text(p.get("Publisher")) or [""])[0],
            "license": select_text(p.get("License")), "description": p.get("Description") or "",
            "provides": p.get("Provides") or "", "mandatory": bool(p.get("Mandatory")),
        }, "versions": []})["versions"].append({
            "version": v.get("Version") or "", "channel": select_text(v.get("Channel")),
            "minEngine": v.get("MinEngine") or "", "maxEngine": v.get("MaxEngine") or "",
            "abiTag": v.get("AbiTag") or "", "iconSha": v.get("IconSha") or "",
            "bannerSha": v.get("BannerSha") or "",
            "requires": json.loads(v.get("Requires") or "[]"),
            "components": comps,
        })
    return entries, revoked


# ------------------------------------------------------------------ seed
def from_seed(cfg):
    entries, revoked = {}, []
    for path in sorted(glob.glob(os.path.join(ROOT, "seed", "*.json"))):
        with io.open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if not d.get("valid", True):
            sys.stderr.write("skip %s: package failed validation\n" % os.path.basename(path))
            continue
        p = d["package"]
        comps = [{"name": c["name"], "sha256": c["sha256"], "bytes": c["bytes"],
                  "optional": c["optional"], "label": c.get("label")}
                 for c in d["components"] if c.get("sha256")]
        comps.sort(key=lambda c: c["sha256"])
        icon = next((c["sha256"] for c in comps if c["name"] == "icon"), "")
        banner = next((c["sha256"] for c in comps if c["name"] == "banner"), "")
        entries.setdefault(p["Id"], {"package": {
            "id": p["Id"], "displayName": p.get("DisplayName") or "",
            "category": p.get("Category") or "", "publisher": p.get("Publisher") or "",
            "license": p.get("License") or "", "description": p.get("Description") or "",
            "provides": p.get("Provides") or "", "mandatory": False,
        }, "versions": []})["versions"].append({
            "version": p.get("Version") or "", "channel": "stable",
            "minEngine": p.get("MinEngineVersion") or "", "maxEngine": p.get("MaxEngineVersion") or "",
            "abiTag": p.get("AbiTag") or "", "iconSha": icon, "bannerSha": banner,
            "requires": d.get("requires", []), "components": comps,
        })
    return entries, revoked


# ------------------------------------------------------------------ render
def esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def render(cfg, entries, revoked, stamp=None):
    L = ["// Generated by tools/render_index.py - do not edit by hand.",
         "// Every package here has an Approved version row in Baserow.", "",
         "IndexInfo", "{",
         '\tstring %%Schema{"%s"}' % esc(cfg["schema"]),
         '\tstring %%Registry{"%s"}' % esc(cfg["registry"]),
         '\tstring %%BlobBase{"%s"}' % esc(cfg["blobBase"]),
         '\tstring %%BlobLayout{"%s"}' % esc(cfg["blobLayout"])]
    if stamp:
        L.append('\tstring %%Generated{"%s"}' % esc(stamp))
    L += ["}", ""]

    for pid in sorted(entries):
        e = entries[pid]
        p = e["package"]
        L += ["Package", "{",
              '\tstring %%Id{"%s"}' % esc(p["id"]),
              '\tstring %%DisplayName{"%s"}' % esc(p["displayName"]),
              '\tstring %%Category{"%s"}' % esc(p["category"]),
              '\tstring %%Publisher{"%s"}' % esc(p["publisher"]),
              '\tstring %%License{"%s"}' % esc(p["license"]),
              '\tstring %%Description{"%s"}' % esc(p["description"])]
        if p.get("provides"):
            L.append('\tstring %%Provides{"%s"}' % esc(p["provides"]))
        if p.get("mandatory"):
            L.append('\tbool %Mandatory{true}')

        for v in sorted(e["versions"], key=lambda x: x["version"]):
            L += ["", "\tVersion", "\t{",
                  '\t\tstring %%Version{"%s"}' % esc(v["version"]),
                  '\t\tstring %%Channel{"%s"}' % esc(v["channel"]),
                  '\t\tstring %%MinEngine{"%s"}' % esc(v["minEngine"]),
                  '\t\tstring %%MaxEngine{"%s"}' % esc(v["maxEngine"]),
                  '\t\tstring %%AbiTag{"%s"}' % esc(v["abiTag"])]
            if v.get("iconSha"):
                L.append('\t\tstring %%IconSha{"%s"}' % esc(v["iconSha"]))
            if v.get("bannerSha"):
                L.append('\t\tstring %%BannerSha{"%s"}' % esc(v["bannerSha"]))
            for r in sorted(v.get("requires", []), key=lambda x: x.get("id", "")):
                L.append('\t\tRequires { string %%Id{"%s"} string %%Range{"%s"} }'
                         % (esc(r.get("id", "")), esc(r.get("range", ""))))
            for c in v["components"]:
                extra = " bool %Optional{true}" if c.get("optional") else ""
                if c.get("label"):
                    extra += ' string %%Label{"%s"}' % esc(c["label"])
                L.append('\t\tComponent { string %%Name{"%s"} string %%Sha256{"%s"} '
                         'int32 %%Bytes{%d}%s }' % (esc(c["name"]), esc(c["sha256"]), c["bytes"], extra))
            L.append("\t}")
        L += ["}", ""]

    for key, reason in sorted(revoked):
        L.append('Revoked { string %%Key{"%s"} string %%Reason{"%s"} }' % (esc(key), esc(reason)))
    if revoked:
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Render the package index.")
    ap.add_argument("--out", default=os.path.join(ROOT, "site", "index.plPackageIndex"))
    ap.add_argument("--source", choices=["auto", "baserow", "seed"], default="auto")
    ap.add_argument("--stamp", metavar="ISO8601",
                    help="embed a Generated timestamp; omit to keep output byte-stable")
    ap.add_argument("--allow-empty", action="store_true",
                    help="permit an index with no packages (normally a misconfiguration)")
    args = ap.parse_args()

    cfg = load_config()
    token = os.environ.get("BASEROW_TOKEN", "").strip()
    host = (cfg["baserow"].get("baseUrl") or "").strip()
    missing = missing_tables(cfg)

    source = args.source
    if source == "auto":
        source = "baserow" if (token and host and not missing) else "seed"
        if source == "seed":
            why = ("BASEROW_URL not set" if not host else
                   "BASEROW_TOKEN not set" if not token else
                   "table ids not set: " + ", ".join(missing))
            print("using seed/ (%s)" % why)
    if source == "baserow":
        if not host:
            sys.exit("BASEROW_URL is not set")
        if not token:
            sys.exit("BASEROW_TOKEN is not set")
        if missing:
            sys.exit("these table ids are still 0 in registry.config.json: %s\ncurrent: %s"
                     % (", ".join(missing), describe_tables(cfg)))
        entries, revoked = from_baserow(cfg, token)
    else:
        entries, revoked = from_seed(cfg)

    # An index that renders to nothing is almost always a misconfiguration - a
    # catalogue with no Approved rows yet, or the wrong table ids - rather than a
    # deliberate purge. Deploying it would silently unpublish every package, so
    # refuse unless it is explicitly asked for.
    if not entries and not args.allow_empty:
        sys.exit(
            "refusing to write an index with no packages (source: %s).\n"
            "  If the catalogue is not populated yet, render from the seed instead:\n"
            "      python tools/render_index.py --source seed\n"
            "  If you really do mean to publish an empty registry, pass --allow-empty."
            % source)

    text = render(cfg, entries, revoked, args.stamp)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")

    versions = sum(len(e["versions"]) for e in entries.values())
    blobs = sum(len(v["components"]) for e in entries.values() for v in e["versions"])
    print("source   %s" % source)
    print("packages %d, versions %d, components %d, revoked %d"
          % (len(entries), versions, blobs, len(revoked)))
    print("wrote    %s (%d bytes)" % (args.out, len(text) + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
