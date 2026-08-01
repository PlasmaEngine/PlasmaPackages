#!/usr/bin/env python3
"""Sign the index with an Ed25519 key, producing a detached signature.

    python tools/sign_index.py site/index.plPackageIndex

Reads the private key from REGISTRY_SIGNING_KEY (a PEM PKCS#8 block, or a path
to one) and writes <index>.sig next to the input, plus site/registry.pub.

Why this matters: blob hashes make the artifacts tamper-evident, but only if the
hashes themselves are trustworthy. Anything that can rewrite the index can point
a package id at a blob of its choosing, and the client would verify it happily.
The signature is what closes that gap.

Generate a keypair with:
    python tools/sign_index.py --generate
Put the printed private key in the REGISTRY_SIGNING_KEY secret, commit the
public half, and embed it in the engine.
"""

import argparse, io, os, sys

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    sys.exit("needs the 'cryptography' package:  python -m pip install cryptography")


def generate():
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    print("# ---- private: put this in the REGISTRY_SIGNING_KEY secret ----")
    print(priv)
    print("# ---- public: commit as site/registry.pub and embed in the engine ----")
    print(pub)
    print("# Store the private key somewhere you can recover it. Losing it means")
    print("# re-keying every engine build that trusts the current public half.")


def load_key():
    raw = os.environ.get("REGISTRY_SIGNING_KEY", "").strip()
    if not raw:
        sys.exit("REGISTRY_SIGNING_KEY is not set")
    if os.path.isfile(raw):
        with io.open(raw, "rb") as fh:
            raw = fh.read()
    else:
        raw = raw.encode()
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit("REGISTRY_SIGNING_KEY is not an Ed25519 private key")
    return key


def main():
    ap = argparse.ArgumentParser(description="Sign the package index.")
    ap.add_argument("index", nargs="?", help="path to index.plPackageIndex")
    ap.add_argument("--generate", action="store_true", help="print a fresh keypair and exit")
    args = ap.parse_args()

    if args.generate:
        generate()
        return 0
    if not args.index:
        ap.error("index path is required (or pass --generate)")

    key = load_key()
    with io.open(args.index, "rb") as fh:
        payload = fh.read()

    sig = key.sign(payload)
    with io.open(args.index + ".sig", "wb") as fh:
        fh.write(sig)

    pub_path = os.path.join(os.path.dirname(os.path.abspath(args.index)), "registry.pub")
    with io.open(pub_path, "wb") as fh:
        fh.write(key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo))

    print("signed %s (%d bytes) -> %s.sig" % (args.index, len(payload), args.index))
    print("public key -> %s" % pub_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
