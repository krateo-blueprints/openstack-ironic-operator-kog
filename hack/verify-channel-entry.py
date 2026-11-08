#!/usr/bin/env python3
"""Verify that the blueprints channel advertises a FETCHABLE chart.

The consumer of that index is anonymous: the Krateo portal's Marketplace and
`helm` reach the `.tgz` with no credentials. Every other check in
release-chart.yaml authenticates, so none of them can see the failure this one
is for -- a publish that concludes SUCCESS and leaves a card whose chart 404s.

WHY THIS EXISTS, with the wrong reason removed. It was written on the premise
that "this repository is private, so its release assets are not anonymously
fetchable". THAT WAS FALSE -- `origin` is
krateo-blueprints/openstack-ironic-operator-kog, which is PUBLIC; `gh` had
resolved a different repo via an `upstream` remote. The premise is retracted in
docs/MARKETPLACE.md and the gate built on it was deleted.

The check survives its own justification, and 2026-11-07 proved why. Tag 0.3.1
had a token that was present AND authenticated as a header, and the publish
still failed because the token was unusable inside a URL. Tag 0.3.2 then
succeeded, and this script read the result independently of the job that
produced it:

    kubernetes-cluster 0.14.0 -> .../charts/releases/download/... http=200
    baremetal-host     0.6.3  -> .../charts/releases/download/... http=200

The durable point is the one the false premise was reaching for: every other
check in that pipeline authenticates, and the consumer does not.

This script asserts nothing about WHICH host the index names. It reads the URL
the index actually advertises and fetches that, so it stays correct across
krateo-blueprints/charts#6 (which moves the store to that repo's own public
Releases) and anything after it.

Usage:  verify-channel-entry.py <chart>=<version> [<chart>=<version> ...]
        verify-channel-entry.py --index <url|path> kubernetes-cluster=0.14.0

Exit 0 only if every pair is present in the index AND its first url returns 200
to an unauthenticated request.
"""
import argparse
import sys
import time
import urllib.error
import urllib.request

import yaml

DEFAULT_INDEX = "https://krateo-blueprints.github.io/charts/blueprints/index.yaml"
# GitHub Pages serves the index from a deploy that lags the publisher's push to
# gh-pages. Waiting is not optional: without it a fresh publish reports a false
# absence, which reads as "the publish failed" when it did not.
ATTEMPTS = 10
SLEEP_S = 20


def fetch(url, timeout=60):
    """Unauthenticated GET. No credentials are added anywhere in this file."""
    req = urllib.request.Request(url, headers={"User-Agent": "verify-channel-entry"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def load_index(src):
    if "://" in src:
        _, body = fetch(src)
    else:
        body = open(src, "rb").read()
    doc = yaml.safe_load(body)
    if not isinstance(doc, dict) or "entries" not in doc:
        # RuntimeError, NOT SystemExit. SystemExit derives from BaseException, so
        # `except Exception` around the caller did not catch it and the retry
        # loop -- whose whole purpose is the lagging-Pages case -- terminated on
        # attempt 1 of 10. A Pages deploy serving a holding page answers 200 with
        # an HTML body, which parses to a str and lands exactly here. Found by
        # /code-review.
        raise RuntimeError(f"{src} is not a Helm index (no 'entries' key)")
    return doc


def advertised_url(index, chart, version):
    for entry in index.get("entries", {}).get(chart, []) or []:
        if str(entry.get("version")) == version:
            urls = entry.get("urls") or []
            return urls[0] if urls else None
    return None


def head_status(url):
    """A real HEAD, with a GET fallback.

    This used to call fetch(), which does r.read() on the whole body just to
    look at r.status -- 80 KB for kubernetes-cluster, per attempt. Some servers
    refuse HEAD (405/501), so fall back rather than reporting a refusal as a
    missing asset.
    """
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "verify-channel-entry"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code in (405, 501):
            try:
                status, _ = fetch(url)
                return status
            except urllib.error.HTTPError as e2:
                return e2.code
            except Exception as e2:  # noqa: BLE001
                print(f"  transport error on GET fallback: {e2}", file=sys.stderr)
                return 0
        return e.code
    except Exception as e:  # noqa: BLE001 - any transport failure is a failure
        print(f"  transport error: {e}", file=sys.stderr)
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", metavar="chart=version")
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--attempts", type=int, default=ATTEMPTS)
    ap.add_argument("--sleep", type=float, default=SLEEP_S)
    a = ap.parse_args()

    wanted = []
    for p in a.pairs:
        if "=" not in p:
            raise SystemExit(f"::error::expected chart=version, got {p!r}")
        name, _, ver = p.partition("=")
        # A caller that forgets to quote an array expansion hands the whole
        # list over as ONE argument. That blob still contains an "=", so it
        # parses, and the run then reports a confusing "not in the index" for a
        # chart nobody asked about. Refuse it instead.
        if not name or not ver or any(c.isspace() for c in name + ver):
            raise SystemExit(
                f"::error::{p!r} is not a single chart=version pair. If this came "
                f"from a shell loop, expand the array quoted: \"${{pairs[@]}}\"."
            )
        wanted.append((name, ver))

    # ONE index fetch per attempt, checked against every outstanding pair. It
    # used to be one fetch per pair per attempt -- two charts x 10 attempts = 20
    # downloads and 20 parses of a 1.6 MB document -- and the two waits were
    # SEQUENTIAL, so a pair absent for all 10 attempts made the next pair start
    # its own full wait. /code-review caught both.
    found = {}
    outstanding = list(wanted)
    for i in range(a.attempts):
        try:
            index = load_index(a.index)
        except Exception as e:  # noqa: BLE001 - RuntimeError, transport, parse
            print(f"  index unreadable ({e})", file=sys.stderr)
            index = None
        if index is not None:
            still = []
            for name, ver in outstanding:
                url = advertised_url(index, name, ver)
                if url:
                    found[(name, ver)] = url
                else:
                    still.append((name, ver))
            outstanding = still
        if not outstanding:
            break
        if i + 1 < a.attempts:
            time.sleep(a.sleep)

    failed = False
    for name, ver in wanted:
        url = found.get((name, ver))
        if not url:
            print(
                f"::error::{name} {ver} is not in {a.index} after "
                f"{a.attempts} attempts. The channel job concluded success, so this "
                f"is a publisher-side or Pages-side problem, not a token one."
            )
            failed = True
            continue
        code = head_status(url)
        print(f"{name} {ver} -> {url}  http={code}")
        if code != 200:
            print(
                f"::error::{name} {ver} is in the index but its .tgz is not "
                f"anonymously fetchable (http={code}). The card will render and the "
                f"install will fail. See docs/MARKETPLACE.md."
            )
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
