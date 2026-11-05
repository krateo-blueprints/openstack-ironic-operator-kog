#!/usr/bin/env python3
"""Screen create-body schemas for the plumbing v1.26.x defaulting predicate.

PREDICATE (from oasgen, who is holding a plumbing v1.14.2 -> v1.26.x bump):
an OPTIONAL object property -- not named in its parent's `required` -- that has
a REACHABLE DEFAULTED DESCENDANT, and itself declares NO required properties.

At v1.26.1 such a parent gains `default: {}` in the generated CRD; at v1.14.2 it
gets nothing. The apiserver applies defaults on READ from etcd, so after a
re-render an existing CR would show that object with no user edit -- and rdc
sends top-level spec values whole, so the next reconcile could put a stanza on
the wire that nobody authored. It would land at the next re-render rather than at
the upgrade, so cause and symptom are separated by the #453 regeneration gap.

A zero here is only worth having with a control, so --self-test builds three
schemas that MUST trip it and two that must NOT, and the real scan refuses to
report unless the control passes. Four instances today of a setup that could not
produce the state it tested; this screen will not be the fifth.
"""
import glob
import json
import os
import re
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


COMPOSE = ("allOf", "anyOf", "oneOf")


def members(node, root):
    """node itself plus every composition member, each $ref-resolved.

    The composition keywords are why the FIRST version of this screen was wrong,
    and the fault was not that I forgot them -- it is that both of my controls
    used inline `properties`, because both came from my own model of the shape.
    A control drawn from your own model tests the implementation against the
    model, not against the world. The field shape, reported by the aruba fleet,
    is `allOf: [{$ref: .../PropertiesDto}]` with `nullable: true` -- and a screen
    walking only `properties`/`items` reports 0 on it.
    """
    out = [node]
    for k in COMPOSE:
        for m in (node.get(k) or []):
            md = deref(m, root)
            if isinstance(md, dict):
                out.append(md)
                out.extend(members(md, root)[1:])
    return out


def merged_props(node, root):
    """properties and required unioned across node + its composition members."""
    props, req = {}, set()
    for m in members(node, root):
        props.update(m.get("properties") or {})
        req |= set(m.get("required") or [])
    return props, req


def deref(node, root, seen=None):
    seen = seen or set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            return {}
        seen.add(ref)
        if not ref.startswith("#/"):
            return {}
        cur = root
        for part in ref[2:].split("/"):
            cur = (cur or {}).get(part) if isinstance(cur, dict) else None
            if cur is None:
                return {}
        node = cur
    return node if isinstance(node, dict) else {}


def has_defaulted_descendant(node, root, depth=0, seen=None):
    """True if any descendant (through properties/items/$ref) carries a default."""
    if depth > 24:
        return False
    seen = seen or set()
    node = deref(node, root)
    if not isinstance(node, dict):
        return False
    props, _ = merged_props(node, root)
    for name, sub in props.items():
        sub_d = deref(sub, root)
        if "default" in sub_d:
            return True
        key = id(sub_d)
        if key in seen:
            continue
        seen.add(key)
        if has_defaulted_descendant(sub_d, root, depth + 1, seen):
            return True
    items = node.get("items")
    if items is not None:
        it = deref(items, root)
        if "default" in it:
            return True
        if has_defaulted_descendant(it, root, depth + 1, seen):
            return True
    return False


def walk(node, root, path, parent_required, out, depth=0):
    if depth > 24:
        return
    node = deref(node, root)
    if not isinstance(node, dict):
        return
    props, req = merged_props(node, root)
    for name, sub in props.items():
        sub_d = deref(sub, root)
        t = sub_d.get("type")
        sub_props, _ = merged_props(sub_d, root)
        is_obj = t == "object" or (t is None and bool(sub_props))
        here = f"{path}.{name}"
        if is_obj:
            optional = name not in req
            # MERGED required, not the node's own: a composition member can
            # supply `required`, and reading only sub_d["required"] misses it.
            # Caught by the field-shape control below, which is what controls
            # drawn from outside your own model are for.
            _, own_req_set = merged_props(sub_d, root)
            own_req = sorted(own_req_set)
            if optional and not own_req and has_defaulted_descendant(sub_d, root):
                out.append(here)
        walk(sub_d, root, here, req, out, depth + 1)


def create_bodies(doc):
    """(label, schema) for every create-ish request body in an OAS document."""
    found = []
    for p, item in (doc.get("paths") or {}).items():
        for verb, op in (item or {}).items():
            if verb not in ("post", "put", "patch"):
                continue
            content = ((op or {}).get("requestBody") or {}).get("content") or {}
            for ct, c in content.items():
                sc = c.get("schema")
                if sc:
                    found.append((f"{verb.upper()} {p} [{ct}]", sc))
    return found


def scan_doc(path):
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    hits = []
    for label, sc in create_bodies(doc):
        out = []
        walk(sc, doc, "body", set(), out)
        for h in out:
            hits.append((label, h))
    return hits, len(create_bodies(doc))


SELF_TEST = [
    # (name, schema, must_trip)
    ("optional object, no own required, defaulted child", {
        "type": "object", "properties": {
            "outer": {"type": "object", "properties": {
                "inner": {"type": "string", "default": "x"}}}}}, True),
    ("optional object, defaulted GRANDchild", {
        "type": "object", "properties": {
            "outer": {"type": "object", "properties": {
                "mid": {"type": "object", "properties": {
                    "leaf": {"type": "integer", "default": 1}}}}}}}, True),
    ("optional object, defaulted child under items", {
        "type": "object", "properties": {
            "outer": {"type": "object", "properties": {
                "list": {"type": "array", "items": {"type": "object", "properties": {
                    "leaf": {"type": "string", "default": "y"}}}}}}}}, True),
    ("REQUIRED object with defaulted child -> must NOT trip", {
        "type": "object", "required": ["outer"], "properties": {
            "outer": {"type": "object", "properties": {
                "inner": {"type": "string", "default": "x"}}}}}, False),
    ("optional object WITH its own required -> must NOT trip", {
        "type": "object", "properties": {
            "outer": {"type": "object", "required": ["inner"], "properties": {
                "inner": {"type": "string", "default": "x"}}}}}, False),
    # Drawn from the field, not from my model: the aruba fleet's actual shape.
    # A screen walking only properties/items reports 0 on this.
    ("FIELD SHAPE: allOf+$ref, nullable, defaulted `default` property", {
        "type": "object",
        "properties": {
            "properties": {"allOf": [{"$ref": "#/components/schemas/PropertiesDto"}],
                           "nullable": True}},
        "components": {"schemas": {"PropertiesDto": {
            "type": "object",
            "properties": {"default": {"type": "boolean", "default": False}}}}},
    }, True),
    ("FIELD SHAPE but parent REQUIRED -> must NOT trip", {
        "type": "object", "required": ["properties"],
        "properties": {
            "properties": {"allOf": [{"$ref": "#/components/schemas/PropertiesDto"}]}},
        "components": {"schemas": {"PropertiesDto": {
            "type": "object",
            "properties": {"default": {"type": "boolean", "default": False}}}}},
    }, False),
    ("composition member supplies `required` -> must NOT trip", {
        "type": "object",
        "properties": {
            "outer": {"allOf": [{"$ref": "#/components/schemas/R"}]}},
        "components": {"schemas": {"R": {
            "type": "object", "required": ["inner"],
            "properties": {"inner": {"type": "string", "default": "x"}}}}},
    }, False),
    ("optional object, NO defaulted descendant -> must NOT trip", {
        "type": "object", "properties": {
            "outer": {"type": "object", "properties": {
                "inner": {"type": "string"}}}}}, False),
]


def self_test():
    fails = []
    for name, sc, must in SELF_TEST:
        out = []
        walk(sc, sc, "body", set(), out)
        tripped = bool(out)
        if tripped != must:
            fails.append(f"{name}: tripped={tripped} expected={must}")
    for f in fails:
        print(f"    FAIL {f}")
    print(f"  self-test: {len(SELF_TEST)} cases, {len(fails)} failure(s)")
    return not fails


def main():
    if "--self-test" in sys.argv:
        return 0 if self_test() else 2
    if not self_test():
        print("  CONTROL FAILED -- the scan below would be meaningless. Refusing.")
        return 2
    print()
    total_hits = 0
    for f in sorted(glob.glob(os.path.join(REPO, "oas", "*.yaml"))):
        hits, nbodies = scan_doc(f)
        tag = os.path.basename(f)
        if hits:
            total_hits += len(hits)
            print(f"  *** {tag}: {len(hits)} hit(s) across {nbodies} request body/ies")
            for label, h in hits:
                print(f"        {label}  ->  {h}")
        else:
            print(f"  {tag:32} 0 hits ({nbodies} request body/ies scanned)")
    print(f"\n  TOTAL matching the v1.26.x defaulting predicate: {total_hits}")
    if total_hits == 0:
        print("  Zero, with the control passing -- so the bump would add no `default: {}`")
        print("  to any object in these create bodies.")
    return 1 if total_hits else 0


if __name__ == "__main__":
    sys.exit(main())
