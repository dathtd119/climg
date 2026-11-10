"""containerlab topology parsing (image precedence) and per-image classification."""

import os

import yaml

from . import dockerx
from .util import warn

BUILT, CACHED, PULLABLE, VENDOR, REGISTRY, MISSING = (
    "BUILT", "BUILDABLE-CACHED", "PULLABLE", "VENDOR-FILE", "REGISTRY", "MISSING")


def find_topologies(path):
    """Yield .clab.yml/.clab.yaml files under path (a file or a directory, recursive). Unreadable paths are
    skipped with a warning."""
    if os.path.isfile(path):
        yield path
        return
    if not os.path.isdir(path):
        warn(f"{path}: not found")
        return

    def onerr(err):
        warn(f"skipping unreadable path {err.filename}: {err.strerror}")

    for root, dirs, files in os.walk(path, onerror=onerr):
        dirs.sort()
        for fn in sorted(files):
            if fn.endswith((".clab.yml", ".clab.yaml")):
                yield os.path.join(root, fn)


def resolve_nodes(doc):
    """[(node, kind, image)] with containerlab precedence node > groups > topology.kinds > defaults.
    kind: node > group > defaults; image: node > group > kinds[kind] > defaults."""
    topo = (doc or {}).get("topology") or {}
    defaults = topo.get("defaults") or {}
    kinds = topo.get("kinds") or {}
    groups = topo.get("groups") or {}
    out = []
    for name, node in (topo.get("nodes") or {}).items():
        node = node or {}
        grp = groups.get(node.get("group")) or {}
        kind = node.get("kind") or grp.get("kind") or defaults.get("kind")
        image = (node.get("image") or grp.get("image") or ((kinds.get(kind) or {}).get("image")) or
                 defaults.get("image"))
        out.append((name, kind, image))
    return out


def parse_topology(path):
    """{file, name, nodes:[{node, kind, image}]} or None when unreadable/invalid."""
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as ex:
        warn(f"{path}: cannot read/parse: {ex}")
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("topology"), dict):
        warn(f"{path}: no topology section; skipped")
        return None
    return {"file": path, "name": doc.get("name"),
            "nodes": [{"node": n, "kind": k, "image": i} for n, k, i in resolve_nodes(doc)]}


def classify(inv, image, kind_name=None):
    """-> dict(state, detail, ref, hint). `image` is the docker reference string from the topology."""
    if not image:
        return {"state": MISSING, "detail": "no image resolved", "ref": None,
                "hint": "set node/group/kinds/defaults image"}
    tag = image if ":" in image.rsplit("/", 1)[-1] else image + ":latest"
    im = inv.tags.get(tag)
    if im:
        return {"state": BUILT, "detail": tag, "ref": tag, "hint": None}
    repo = dockerx.split_tag(image)[0]
    k = inv.kinds.by_repo(repo)
    if k is None and not repo.startswith("vrnetlab/"):
        return {"state": REGISTRY, "detail": tag, "ref": tag, "hint": f"docker pull {tag}"}
    recs = [r for r in inv.all_records() if r.get("tag") == tag]
    if not recs and k is not None:
        ver = tag.rsplit(":", 1)[1]
        if k.family_match:   # huawei: tag = <model>-lh<id>; match on the family, not the positional id
            fam = ver.split("-", 1)[0]
            recs = [r for r in inv.all_records() if r["kind"] == k.name and r.get("family") == fam]
    if recs:
        order = {"local": 0, "labhub": 1}
        for r in sorted(recs, key=lambda r: (inv.status(r)[0] != "cached", order.get(r["source"], 2))):
            st, det = inv.status(r)
            if r["source"] == "local" and st in ("cached", "built"):
                return {"state": VENDOR, "detail": r["path"], "ref": r["key"], "hint": f"climg pull {r['key']} --yes"}
            if r["source"] == "labhub":
                if st == "cached":
                    return {"state": CACHED, "detail": det, "ref": r["key"], "hint": f"climg pull {r['key']} --yes"}
                return {"state": PULLABLE, "detail": f"{r['key']} ({r['size'] and round(r['size'] / 1e6)} MB)",
                        "ref": r["key"], "hint": f"climg pull {r['key']} --yes"}
    return {"state": MISSING, "detail": tag, "ref": None, "hint": missing_hint(inv, k, tag)}


def missing_hint(inv, kind, tag):
    drop = inv.cfg.images_dirs[0]
    if kind is None:
        return f"no climg kind builds {tag}; build it manually or fix the image name"
    ver = tag.rsplit(":", 1)[1]
    try:
        fname = kind.render_target(ver, model=ver.split("-", 1)[0])
    except (KeyError, IndexError):
        fname = kind.example
    ex = f" (vendor naming e.g. {kind.example})" if kind.example and kind.example != fname else ""
    return f"drop {fname}{ex} into {drop}, then `climg add <file> --build --yes`"
