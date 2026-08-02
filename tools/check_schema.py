#!/usr/bin/env python3
"""Check the catalogue schema against what the tooling expects.

    python tools/check_schema.py

Reports every missing field, wrong field type and missing select option across all
four tables, so schema problems surface in one pass instead of one 400 at a time.

Exits non-zero if anything required is missing. Run it before the first publish.

Environment:
    BASEROW_URL, BASEROW_TOKEN   (BASEROW_URL may come from registry.local.json)
"""

import io, json, os, sys, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# field name -> (baserow type, required, select options the tooling writes)
EXPECTED = {
    "publishers": {
        "Name":           ("text",    True,  None),
        "GitHubLogin":    ("text",    False, None),
        "Trust":          ("single_select", False, ["FirstParty", "Verified", "Community"]),
        "CanAutoApprove": ("boolean", False, None),
    },
    "packages": {
        "Id":          ("text",          True,  None),
        "DisplayName": ("text",          True,  None),
        "Category":    ("single_select", True,  None),
        "Description": ("long_text",     True,  None),
        "Publisher":   ("link_row",      True,  None),
        "License":     ("single_select", True,  None),
        "Provides":    ("text",          False, None),
        "Mandatory":   ("boolean",       False, None),
        "Visibility":  ("single_select", True,  ["Public"]),
    },
    "versions": {
        "Key":        ("text",          True,  None),
        "Package":    ("link_row",      True,  None),
        "Version":    ("text",          True,  None),
        "Status":     ("single_select", True,  ["Pending", "Approved", "Rejected", "Yanked"]),
        "Channel":    ("single_select", True,  ["stable", "beta"]),
        "MinEngine":  ("text",          True,  None),
        "MaxEngine":  ("text",          False, None),
        "AbiTag":     ("text",          True,  None),
        "Requires":   ("long_text",     False, None),
        "IconSha":    ("text",          False, None),
        "BannerSha":  ("text",          False, None),
        "PublishedBy": ("link_row",     False, None),
        "YankReason": ("long_text",     False, None),
    },
    "components": {
        "Sha256":   ("text",          True,  None),
        "Version":  ("link_row",      True,  None),
        "Name":     ("single_select", True,  ["manifest", "editor-win64", "runtime-win64",
                                              "assets", "localization", "icon", "banner"]),
        "Platform": ("single_select", True,  ["any", "win64"]),
        "Bytes":    ("number",        True,  None),
        "Optional": ("boolean",       False, None),
        "Label":    ("text",          False, None),
    },
}

# Baserow reports several text-ish types; treat them as interchangeable where it does not matter.
TEXT_LIKE = {"text", "long_text", "url", "email"}


def edit_distance(a, b):
    a, b = a.lower(), b.lower()
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def suggest_rename(wanted, present, claimed):
    """A field that is nearly the wanted name is almost certainly it, misspelled."""
    best, best_d = None, 99
    for name in present:
        if name in claimed:
            continue
        d = edit_distance(wanted, name)
        if d < best_d:
            best, best_d = name, d
    # distance 1-2 catches Catagory/Category, Publishers/Publisher, Licence/License, Versions/Version
    return best if best_d <= 2 else None


def load_config():
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


def fields(base, table_id, token):
    url = "%s/api/database/fields/table/%d/" % (base.rstrip("/"), table_id)
    req = urllib.request.Request(url, headers={"Authorization": "Token %s" % token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", "replace")[:200]
        if ex.code in (401, 403):
            raise SystemExit(
                "listing fields needs a token with read access to this table (%s).\n%s" % (ex.code, body))
        raise SystemExit("fields request failed: %s %s\n%s" % (ex.code, url, body))


def main():
    cfg = load_config()
    base = (cfg["baserow"].get("baseUrl") or "").strip()
    token = os.environ.get("BASEROW_TOKEN", "").strip()
    if not base:
        sys.exit("BASEROW_URL is not set (or add it to registry.local.json)")
    if not token:
        sys.exit("BASEROW_TOKEN is not set")

    problems = 0
    for table, expected in EXPECTED.items():
        tid = cfg["baserow"]["tables"].get(table)
        print("\n%s (table %s)" % (table.upper(), tid or "not configured"))
        if not tid:
            print("  table id is 0 in registry.config.json")
            problems += 1
            continue

        actual = {f["name"]: f for f in fields(base, tid, token)}
        renames = []
        for name, (ftype, required, options) in expected.items():
            f = actual.get(name)
            if not f:
                near = suggest_rename(name, actual, expected)
                if near:
                    renames.append((near, name))
                    print("  %-14s MISSING   rename '%s' -> '%s'" % (name, near, name))
                else:
                    print("  %-14s MISSING%s" % (name, "" if required else "  (optional)"))
                problems += required
                continue

            got = f["type"]
            type_ok = (got == ftype) or (got in TEXT_LIKE and ftype in TEXT_LIKE)
            note = "" if type_ok else "  TYPE is %s, expected %s" % (got, ftype)
            if not type_ok:
                problems += 1

            missing_opts = []
            if options and f.get("select_options") is not None:
                have = {o["value"] for o in f["select_options"]}
                missing_opts = [o for o in options if o not in have]
                if missing_opts:
                    problems += 1

            status = "ok" if (type_ok and not missing_opts) else "PROBLEM"
            print("  %-14s %-8s %s%s" % (name, status, got, note))
            if missing_opts:
                have = sorted(o["value"] for o in f["select_options"])
                print("  %-14s          missing options: %s" % ("", ", ".join(repr(o) for o in missing_opts)))
                print("  %-14s          currently has  : %s" % ("", ", ".join(repr(o) for o in have) or "(none)"))

        extra = [n for n in actual if n not in expected and not n.startswith(("Notes", "Active"))]
        if extra:
            print("  %-14s %s" % ("(also present)", ", ".join(sorted(extra))))

    print("\n%s" % ("schema looks usable" if not problems
                    else "%d problem(s) - fix these before publishing" % problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
