"""Docker queries (read-only): local images, vrnetlab detection, tag->image map."""

import json
import subprocess

from .util import ClimgError

AUTHOR_LABEL = "org.opencontainers.image.authors"
AUTHOR_MARK = "roman@dodin.dev"
VR_ENTRYPOINT = ["uv", "run", "/launch.py"]


def _docker(args):
    try:
        p = subprocess.run(["docker"] + args, text=True, capture_output=True)
    except FileNotFoundError:
        raise ClimgError("docker not found in PATH")
    if p.returncode != 0:
        raise ClimgError(f"docker {' '.join(args[:2])} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def is_vrnetlab(labels, entrypoint):
    """Rule from the brief: built vrnetlab images carry the dodin.dev author label AND the uv launcher entrypoint
    (the vrnetlab-version label is missing on some images, so it is not used)."""
    return AUTHOR_MARK in ((labels or {}).get(AUTHOR_LABEL) or "") and list(entrypoint or []) == VR_ENTRYPOINT


def list_images():
    """[{id, tags[], size, created, labels, entrypoint, vrnetlab, dangling}] for all local images (`-a`)."""
    out = _docker(["image", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"])
    ids = sorted(set(out.split()))
    if not ids:
        return []
    insp = json.loads(_docker(["image", "inspect"] + ids))
    res = []
    for i in insp:
        cfg = i.get("Config") or {}
        tags = [t for t in (i.get("RepoTags") or []) if t != "<none>:<none>"]
        res.append({"id": i["Id"], "tags": tags, "size": i.get("Size"), "created": i.get("Created"),
                    "labels": cfg.get("Labels") or {}, "entrypoint": cfg.get("Entrypoint") or [],
                    "vrnetlab": is_vrnetlab(cfg.get("Labels"), cfg.get("Entrypoint")),
                    "dangling": not tags})
    return res


def tag_map(images):
    """tag -> image dict."""
    m = {}
    for im in images:
        for t in im["tags"]:
            m[t] = im
    return m


def inspect_tag(tag):
    p = subprocess.run(["docker", "image", "inspect", tag], text=True, capture_output=True)
    if p.returncode != 0:
        return None
    return json.loads(p.stdout)[0]


def split_tag(ref):
    """'a/b:c' -> ('a/b', 'c'); registry ports (host:5000/x) are not mistaken for tags."""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"
