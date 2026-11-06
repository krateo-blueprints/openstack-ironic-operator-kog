#!/usr/bin/env python3
"""Set a helm release's status in its storage Secret, for constructing test states.

WHY A SCRIPT: a first attempt set the Secret's `status` LABEL and the state did
not change. Two reasons, and both had to be found the hard way:

  1. The label is only helm's query index. `helm list` reports `info.status` from
     the release DATA -- base64(k8s) of base64(helm) of gzip of JSON. Editing the
     label changes what queries match and nothing about the release.
  2. The label was applied to revision .v2 while an adoption had already created
     .v3, so even the index edit landed on a superseded revision.

Both halves are needed: the data for helm's own reading, and the label so the
query layer still finds it.

This is for CONSTRUCTING a state (e.g. `pending-install`) that otherwise requires
racing an interrupted install. Callers must say in their output that the state is
constructed, because what is under test is a controller's handling of the state
and not how it arose.
"""
import base64
import gzip
import io
import json
import subprocess
import sys


def sh(cmd, inp=None):
    r = subprocess.run(cmd, shell=True, input=inp, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"FAILED: {cmd}\n{r.stderr.strip()[:400]}")
    return r.stdout


def main():
    if len(sys.argv) < 5:
        raise SystemExit("usage: helm-set-release-status.py <kubectl> <ns> <release> <status>")
    kubectl, ns, rel, status = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

    # The CURRENT revision, not whichever one was current when you looked last.
    secs = sh(f"{kubectl} -n {ns} get secret -l name={rel},owner=helm "
              f"-o jsonpath='{{range .items[*]}}{{.metadata.name}} {{end}}'").split()
    if not secs:
        raise SystemExit(f"no helm storage Secret for release {rel} in {ns}")
    cur = max(secs, key=lambda s: int(s.rsplit(".v", 1)[1]))
    print(f"   current revision Secret: {cur}  (of {len(secs)}: {sorted(secs)})")

    raw = sh(f"{kubectl} -n {ns} get secret {cur} -o jsonpath='{{.data.release}}'")
    inner = base64.b64decode(raw)            # k8s layer
    blob = base64.b64decode(inner)           # helm layer
    rel_json = json.loads(gzip.decompress(blob))

    was = (rel_json.get("info") or {}).get("status")
    rel_json.setdefault("info", {})["status"] = status
    print(f"   info.status: {was} -> {status}")

    # Optional 5th arg "now": also stamp info.last_deployed to this instant.
    # The cdc's pending-operation grace is measured from last_deployed, NOT from
    # when the controller first saw the release -- so a constructed pending state
    # with a stale last_deployed is ALREADY past its grace and gets rolled back
    # immediately, skipping the behaviour under test.
    if len(sys.argv) > 5 and sys.argv[5] == "now":
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f000Z")
        prev = rel_json["info"].get("last_deployed")
        rel_json["info"]["last_deployed"] = ts
        print(f"   info.last_deployed: {prev} -> {ts}")

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as fh:
        fh.write(json.dumps(rel_json).encode())
    newdata = base64.b64encode(base64.b64encode(buf.getvalue())).decode()

    patch = json.dumps({"data": {"release": newdata}})
    sh(f"{kubectl} -n {ns} patch secret {cur} --type=merge -p {json.dumps(patch)}")
    sh(f"{kubectl} -n {ns} label secret {cur} status={status} --overwrite")
    print(f"   patched data AND label on {cur}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
