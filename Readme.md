# PlasmaPackages

The package registry for PlasmaEngine plugins.

This repository holds **artifacts and the index generator** — no plugin source. Plugin code lives in its own
repository; this is where the built packages land.

- **Index:** <https://plasmaengine.github.io/PlasmaPackages/index.plPackageIndex>
- **Blobs:** release assets in this repo, sharded by hash

---

## Using the registry

Add it as a source in the editor's Plugin Manager, or by hand:

```
Source
{
	string %Name{"plasma-official"}
	string %Index{"https://plasmaengine.github.io/PlasmaPackages/index.plPackageIndex"}
}
```

The editor fetches the index, resolves what a project needs, and downloads only the components its platform
and role require. Everything is verified against the hashes in the index before it is installed.

---

## How artifacts are stored

Blobs are **not** committed. They are release assets named by SHA-256 and filed into a release tagged
`blobs-<first two hex characters of the hash>`:

```
https://github.com/PlasmaEngine/PlasmaPackages/releases/download/blobs-c5/c56232e3….plpkg
```

The shard falls out of the hash, so a download URL is computable from the hash alone with no lookup, and
nothing has to record where a blob was filed. 256 shards × 1000 assets is 256,000 blobs of headroom.

A package is split into components — editor binaries, runtime binaries per platform, assets, localization,
artwork — so a client fetches only what it needs. Content addressing means an unchanged component is stored
once and shared by every version that uses it.

---

## Layout

```
registry.config.json     registry name, blob base URL, catalogue table ids
seed/                    package JSON used to render the index before the catalogue is populated
site/                    what GitHub Pages serves
  index.plPackageIndex   generated
  index.html             landing page
tools/
  render_index.py        catalogue (or seed) -> index
  publish_package.py     upload blobs, record the release
  sign_index.py          Ed25519 detached signature
.github/workflows/
  render-index.yml       webhook + nightly + manual -> render, sign, deploy Pages
```

---

## Publishing

Packages are built and staged in the engine repository, then published from here:

```bash
# see exactly what would happen, without changing anything
python tools/publish_package.py seed/<package>@<version>.json --dry-run

# upload the blobs and record the release
python tools/publish_package.py seed/<package>@<version>.json
```

The publisher refuses to run on a package that failed validation, skips blobs already present (identical hash
means identical bytes), and always records a new release as **Pending**. Approval is a human action in the
catalogue — no token used by the tooling is permitted to change a release's status.

Republishing an existing `id@version` is a hard error. Overwriting a published version would break every lock
file that already trusts it; fix forward with a new patch version instead.

---

## Regenerating the index

```bash
python tools/render_index.py                   # catalogue if configured, else seed
python tools/render_index.py --source seed     # force seed
python tools/render_index.py --source baserow  # force catalogue, fail if unavailable
```

Output is deterministic — sorted throughout, with no timestamp unless `--stamp` is passed — so an unchanged
catalogue renders byte-identical and CDN caches stay warm.

---

## Maintainer setup

### 1. Pages

Settings → Pages → Source: **GitHub Actions**. Run *Render index* manually once; with no catalogue configured
it renders from `seed/`, which is enough to prove the deploy works.

### 2. Catalogue

Create the catalogue tables, then set the repository variable and secret below. Table ids can be read from the
URL when a table is open.

| Name | Kind | Used by | Needs |
|---|---|---|---|
| `BASEROW_URL` | variable | `render-index.yml` | Base URL of the catalogue instance |
| `BASEROW_TOKEN` | secret | `render-index.yml` | **read** on all four tables — nothing more |
| `REGISTRY_SIGNING_KEY` | secret | `render-index.yml` | Ed25519 private key, PEM. Keep it in a protected Environment |

The publishing token is separate and narrower: create + read on the release tables only. Neither token should
ever be granted update on a release's status, so that no automation can approve its own publish.

### 3. Signing keypair

```bash
python tools/sign_index.py --generate
```

Private half → the `REGISTRY_SIGNING_KEY` secret. Public half → `site/registry.pub`, committed, and embedded
in the engine. Until this exists the index deploys unsigned; once it exists, an unsigned index should be a
build failure rather than a warning — blob hashes only protect you if the hashes themselves are trustworthy.

### 4. Catalogue webhook

Add a webhook on the releases table, firing on row updates:

```
POST https://api.github.com/repos/PlasmaEngine/PlasmaPackages/dispatches
Accept:        application/vnd.github+json
Authorization: Bearer <token with contents:write on this repo>

{ "event_type": "registry-changed" }
```

Keep the nightly schedule regardless — a missed webhook should delay the index by hours, not indefinitely.
