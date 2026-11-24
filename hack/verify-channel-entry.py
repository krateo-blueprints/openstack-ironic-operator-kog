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

try:
    import yaml
except ImportError:
    # A missing dependency measured NOTHING. Exit 3 rather than letting the
    # traceback exit 1, which in THIS file means "a chart is missing or its .tgz
    # is not fetchable" -- an unavailable instrument must never be
    # indistinguishable from a finding. The release step carries a pip fallback
    # for exactly this, and that fallback was dead code until /code-review read
    # it, so the guard has to be here too. Same fix as PR #142, which swept
    # scripts/ and did not reach hack/.
    print("verify-channel-entry: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(3)

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


def resolve(index_src, wanted, attempts, sleep_s):
    """Resolve every pair to the url the index advertises. Returns (found, outstanding)."""
    # `outstanding` governs the RETRY only -- which pairs are still worth
    # looking for, and when to break early. It is NOT what makes this check fail
    # closed; --self-test case 3 proved that by emptying it and changing nothing.
    # What fails closed is that the verdict below is read off `found`, which is
    # POSITIVE evidence: a pair only enters it when an index was actually read
    # and actually advertised it. The publisher's step at
    # publish-chart.yaml@70095ab derives its verdict from `missing` instead,
    # seeded empty before its retry loop (krateo-blueprints/charts#53) -- so
    # reading nothing leaves nothing missing. Same shape, opposite default.
    found = {}
    outstanding = list(wanted)
    for i in range(attempts):
        try:
            index = load_index(index_src)
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
        if i + 1 < attempts:
            time.sleep(sleep_s)
    return found, outstanding


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", metavar="chart=version")
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--attempts", type=int, default=ATTEMPTS)
    ap.add_argument("--sleep", type=float, default=SLEEP_S)
    a = ap.parse_args(argv)

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
    # its own full wait. /code-review caught both. The loop itself now lives in
    # resolve(), so --self-test can drive it and mutate its seeding.
    found, _outstanding = resolve(a.index, wanted, a.attempts, a.sleep)

    # The asset gets the SAME retry budget as the index. It used to get exactly
    # one shot: the loop above broke as soon as every pair was present in the
    # index, then head_status ran once. But the release asset is a SEPARATE write
    # from the gh-pages index write, so the lag the index retry exists for
    # applies to it too -- and a 404 seconds after publication was reported as
    # "in the index but its .tgz is not anonymously fetchable", a hard failure in
    # the only consumer-credentials check in the pipeline. /code-review caught it.
    def status_with_retry(url):
        code = head_status(url)
        for _ in range(a.attempts - 1):
            if code == 200:
                return code
            time.sleep(a.sleep)
            code = head_status(url)
        return code

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
        code = status_with_retry(url)
        print(f"{name} {ver} -> {url}  http={code}")
        if code != 200:
            print(
                f"::error::{name} {ver} is in the index but its .tgz is not "
                f"anonymously fetchable (http={code}). The card will render and the "
                f"install will fail. See docs/MARKETPLACE.md."
            )
            failed = True

    return 1 if failed else 0


# ----------------------------------------------------------------- self-test
# WHY THIS EXISTS, dated 2026-11-24. This file had no test for its whole life.
# It ran in 10 green releases and never failed, which proves nothing: the
# publisher's equivalent step ALSO looked right and turned out to pass on an
# unreadable index, because it seeded its missing-list empty. Reading that a
# check exists is not evidence that it can fail. So every case below is a
# POSITIVE control -- an input this check must reject, with its own reason --
# plus one clean negative control, and one MUTATION arm that removes the line
# doing the catching and requires the outcome to flip.
def _resp(status, body=b""):
    class _R:
        def __enter__(self_):
            return self_
        def __exit__(self_, *a):
            return False
        read = staticmethod(lambda: body)
    r = _R()
    r.status = status
    return r


def selftest():
    import contextlib
    import io
    import tempfile

    g = globals()
    cases, failures, voids = [], [], []

    GOOD = {
        "kubernetes-cluster": [{"version": "0.16.1", "urls": ["https://e.invalid/kc.tgz"]}],
        "baremetal-host": [{"version": "0.7.0", "urls": ["https://e.invalid/bh.tgz"]}],
    }
    PAIRS = ["kubernetes-cluster=0.16.1", "baremetal-host=0.7.0"]
    BASE = ["--attempts", "2", "--sleep", "0", "--index", "https://e.invalid/index.yaml"]

    def run(argv, index_impl, status_impl):
        """Drive main() end to end with the two network seams replaced."""
        saved = (g["load_index"], g["head_status"])
        g["load_index"], g["head_status"] = index_impl, status_impl
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = main(argv)
        except SystemExit as e:  # the arg-shape refusals raise SystemExit(str)
            # The message is the SystemExit's payload and is printed by the
            # interpreter, which never runs here -- so put it in the buffer, or
            # the case asserts an exit code and nothing about the reason.
            if not isinstance(e.code, int):
                buf.write(str(e.code))
            code = e.code if isinstance(e.code, int) else 1
        finally:
            g["load_index"], g["head_status"] = saved
        return code, buf.getvalue()

    def case(label, got, want, text=None, out=""):
        cases.append(label)
        if got != want or (text and text not in out):
            failures.append(
                f"{label}: exit={got} want={want}"
                + (f", missing {text!r} in output" if text and text not in out else "")
            )

    served = lambda entries: (lambda src: {"entries": entries})
    unreadable = lambda src: (_ for _ in ()).throw(RuntimeError("404 while fetching"))

    # 1. negative control: everything present and fetchable
    c, o = run(BASE + PAIRS, served(GOOD), lambda u: 200)
    case("clean index, both charts, http=200 -> pass", c, 0)

    # 2. THE PUBLISHER'S HOLE, on this check: the index never loads
    c, o = run(BASE + PAIRS, unreadable, lambda u: 200)
    case("index NEVER readable -> fail", c, 1, "is not in", o)

    # 3. MUTATION ARM A, and the reason this file now says what it says. The
    #    first version of this arm emptied the retry seeding and expected case 2
    #    to flip to a pass. IT DID NOT FLIP, so the seeding is not what catches
    #    an unreadable index, and the comment that said it was has been removed.
    #    The arm is kept inverted: emptying the seeding must change NOTHING.
    saved_resolve = g["resolve"]
    saved_seed_outcome, _ = run(BASE + PAIRS, unreadable, lambda u: 200)
    cases.append("mutation A: the retry seeding is NOT the guard (emptying it changes nothing)")
    if saved_seed_outcome != 1:
        voids.append("mutation A: the unreadable-index case no longer fails at all (exit=%s)"
                     % saved_seed_outcome)

    # 4. MUTATION ARM B: the real guard. Make resolve() claim it found every
    #    pair -- what a verdict derived from negative evidence would effectively
    #    do -- and the unreadable index must then PASS. That flip is what proves
    #    the verdict is read off `found`.
    g["resolve"] = lambda src, wanted, attempts, sleep_s: (
        {pair: "https://e.invalid/claimed.tgz" for pair in wanted}, [])
    try:
        c, o = run(BASE + PAIRS, unreadable, lambda u: 200)
    finally:
        g["resolve"] = saved_resolve
    cases.append("mutation B: a verdict that trusts 'found' blindly makes case 2 pass")
    if c != 0:
        voids.append("mutation B: claiming every pair found did not flip case 2 (exit=%s), so "
                     "case 2 does not prove the verdict comes from `found`" % c)

    # 4. the index is served but lacks the VERSION we published
    stale = {"kubernetes-cluster": [{"version": "0.16.0", "urls": ["https://e.invalid/old.tgz"]}],
             "baremetal-host": GOOD["baremetal-host"]}
    c, o = run(BASE + PAIRS, served(stale), lambda u: 200)
    case("index lacks the version -> fail", c, 1, "kubernetes-cluster 0.16.1 is not in", o)

    # 5. the index lacks the CHART
    c, o = run(BASE + PAIRS, served({"baremetal-host": GOOD["baremetal-host"]}), lambda u: 200)
    case("index lacks the chart -> fail", c, 1, "kubernetes-cluster 0.16.1 is not in", o)

    # 6. the entry exists at the right version and advertises NO url. Silent
    #    nothing is the shape most likely to read as present.
    nourl = {"kubernetes-cluster": [{"version": "0.16.1", "urls": []}],
             "baremetal-host": GOOD["baremetal-host"]}
    c, o = run(BASE + PAIRS, served(nourl), lambda u: 200)
    case("entry with an empty urls list -> fail", c, 1, "is not in", o)

    # 7. in the index, and the .tgz 404s anonymously -- the card renders, the
    #    install fails. This is the failure the whole file is for.
    c, o = run(BASE + PAIRS, served(GOOD), lambda u: 404)
    case("asset 404 -> fail", c, 1, "not anonymously fetchable", o)

    # 8. a transport failure reports http=0, which must not read as 200
    c, o = run(BASE + PAIRS, served(GOOD), lambda u: 0)
    case("transport failure (http=0) -> fail", c, 1, "not anonymously fetchable", o)

    # 9. the unquoted-array blob: one argv entry containing both pairs. It
    #    parses (it has an "="), and argparse ACCEPTED it once.
    c, o = run(BASE + [" ".join(PAIRS)], served(GOOD), lambda u: 200)
    case("unquoted array expansion -> refused", c, 1, "single chart=version pair", o)

    # 10. UNIT: a Pages holding page answers 200 with HTML, which yaml parses to
    #     a str. load_index must raise RuntimeError (not SystemExit, which the
    #     retry loop cannot catch).
    cases.append("a holding page is not an index -> RuntimeError")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("<html><body>404</body></html>\n")
        holding = fh.name
    try:
        load_index(holding)
        failures.append("a holding page is not an index: load_index accepted HTML")
    except RuntimeError:
        pass
    except Exception as e:  # noqa: BLE001
        failures.append(f"a holding page is not an index: raised {type(e).__name__}, want RuntimeError")

    # 11. UNIT: a server that refuses HEAD (405) must fall back to GET, or a
    #     refusal is reported as a missing asset.
    cases.append("HEAD refused with 405 -> GET fallback reports 200")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.get_method())
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, None)
        return _resp(200, b"x")

    saved_open = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        code = head_status("https://e.invalid/kc.tgz")
    finally:
        urllib.request.urlopen = saved_open
    if code != 200 or calls != ["HEAD", "GET"]:
        failures.append(f"HEAD 405 fallback: code={code} calls={calls}, want 200 and HEAD then GET")

    print(f"  self-test: {len(cases)} cases, {len(failures)} failure(s), {len(voids)} void(s)")
    for f in failures:
        print(f"    FAIL {f}")
    for v in voids:
        print(f"    VOID {v}")
    if voids:
        print("  VOID is not a pass: a control that cannot be shown to bite is not a control.")
        return 3
    if not failures:
        print("  -> every arm above is an input this check REJECTS; the verdict is read "
              "off POSITIVE evidence (`found`), and mutation B flips the "
              "unreadable-index arm to a pass by taking that away.")
        print("  -> this harness has been seen to report failures: on 2026-11-24 it "
              "returned 1 failure and 1 void against this same file, which is how "
              "arms A and B reached their current form.")
    return 1 if failures else 0


# MODE FLAGS: both spellings accepted, an unrecognised `--`-prefixed first
# argument REFUSED. This file takes POSITIONAL chart=version pairs and real
# options, so it cannot refuse unknown arguments wholesale the way the screens in
# scripts/ do -- it uses the same shape as check-schema-warnings.py, which takes a
# kubectl invocation. Without this, a mistyped `--self-tet` reaches argparse and
# exits 2 with a generic "unrecognised arguments", which reads as a broken caller
# rather than as "your mode flag did not take". scripts/check-screen-flags.py
# classifies every self-test's flag handling and calls an unrecognised shape VOID,
# which is how this got written -- it found this file the moment the self-test
# existed.
_SELFTEST = ("--self-test", "--selftest")
_OPTIONS = ("--index", "--attempts", "--sleep", "--help", "-h")

if __name__ == "__main__":
    _first = sys.argv[1] if len(sys.argv) > 1 else ""
    if _first.startswith("--") and _first not in _SELFTEST + _OPTIONS:
        sys.stderr.write("usage: %s [%s] | <chart>=<version> [...] [%s ...]\n"
                         % (sys.argv[0], "|".join(_SELFTEST), "|".join(_OPTIONS[:3])))
        sys.stderr.write("refusing unrecognised first argument: %s\n" % _first)
        sys.stderr.write("a mistyped mode flag would otherwise look like a caller error.\n")
        sys.exit(64)
    # The self-test runs as its own invocation so the Makefile can put it FIRST:
    # a check whose controls did not run has not been shown to work.
    sys.exit(selftest() if set(sys.argv[1:]) & set(_SELFTEST) else main())
