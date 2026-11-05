#!/usr/bin/env python3
"""Mint a system-scoped Keystone token and patch it into the Secret the
<Kind>Configuration CRs reference via `tokenRef`.

Replaces a /bin/sh version that did `apk add --no-cache curl jq` at runtime.
That install wrote to the root filesystem, so the container could never run
with `readOnlyRootFilesystem: true` (Trivy KSV-0014) -- and it also meant a
token refresh failed whenever the alpine mirror was unreachable, on a path
whose whole job is keeping credentials current.

Stdlib only, on python:3.13-slim -- the same image `ironic-extra-bridge`
already uses in this repo, so no new image enters the supply chain.

Two things the shell version got right that are easy to lose in a rewrite,
kept deliberately:

  * the token is in the **X-Subject-Token response header**, not in the body.
    That is the reason External Secrets Operator cannot stand in for this.
  * the auth body is built by a JSON serialiser, never by string
    interpolation. `jq --arg` was doing real work: a password containing a
    quote or backslash would corrupt a hand-built body. `json.dumps` is the
    equivalent guarantee.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

CREDS = "/creds"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
KAPI = "https://kubernetes.default.svc"
NAMESPACE = os.environ.get("TOKEN_SECRET_NAMESPACE", "openstack")
SECRET = os.environ.get("TOKEN_SECRET_NAME", "ironic-keystone-token")
TIMEOUT = 20


def read(path):
    with open(path) as fh:
        return fh.read().strip()


def main():
    auth_url = read(f"{CREDS}/authUrl")
    body = json.dumps({
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {"user": {
                    "name": read(f"{CREDS}/username"),
                    "password": read(f"{CREDS}/password"),
                    "domain": {"name": read(f"{CREDS}/userDomain")},
                }},
            },
            # System-scoped on purpose: project scope returns 403 on
            # baremetal:port:create. See the manifest header.
            "scope": {"system": {"all": True}},
        }
    }).encode()

    req = urllib.request.Request(
        f"{auth_url}/v3/auth/tokens", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            # The token is a HEADER. Reading it from the body would silently
            # yield nothing.
            token = resp.headers.get("X-Subject-Token")
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        # Never echo the response body: it can carry request detail. The shell
        # version redacted with a regex; not emitting it at all is stronger.
        print(f"keystone auth failed: HTTP {e.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"keystone unreachable: {e.reason}", file=sys.stderr)
        return 1

    if not token:
        print("no X-Subject-Token in the response", file=sys.stderr)
        return 1
    expires = (payload.get("token") or {}).get("expires_at", "unknown")
    print(f"minted token, expires_at={expires}, length={len(token)}")

    patch = json.dumps({"data": {
        "token": base64.b64encode(token.encode()).decode()}}).encode()
    preq = urllib.request.Request(
        f"{KAPI}/api/v1/namespaces/{NAMESPACE}/secrets/{SECRET}",
        data=patch, method="PATCH",
        headers={"Content-Type": "application/strategic-merge-patch+json",
                 "Authorization": f"Bearer {read(f'{SA}/token')}"})
    import ssl
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    try:
        with urllib.request.urlopen(preq, timeout=TIMEOUT, context=ctx) as resp:
            if resp.status != 200:
                print(f"secret patch failed: HTTP {resp.status}", file=sys.stderr)
                return 1
    except urllib.error.HTTPError as e:
        print(f"secret patch failed: HTTP {e.code}", file=sys.stderr)
        return 1

    print(f"{NAMESPACE}/{SECRET} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
