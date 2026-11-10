"""Local state: vendor-supplied image registry, build records, vendor drop-dir discovery, results log."""

import json
import os

from .util import ClimgError, hash_file, now


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".new"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_registry(cfg):
    return _load(cfg.registry_file, {"entries": {}})["entries"]


def register(cfg, entry):
    data = _load(cfg.registry_file, {"entries": {}})
    data["entries"][entry["key"]] = entry
    _save(cfg.registry_file, data)


def load_builds(cfg):
    return _load(cfg.builds_file, {})


def record_build(cfg, tag, info):
    b = load_builds(cfg)
    b[tag] = {**info, "built_at": now().isoformat(timespec="seconds")}
    _save(cfg.builds_file, b)


def local_record(kinds, filename, path, kind=None, ver=None, target=None, size=None, extra=None):
    """Registry-shaped record for a vendor file. Returns None when kind/version cannot be determined."""
    hit = kinds.detect_vendor_file(filename, kind)
    if hit is None and kind and ver:
        k = kinds.find(kind)
        hit = {"kind": k.name, "ver": ver, "target": k.render_target(ver), "groups": {"ver": ver}}
    if hit is None:
        return None
    k = kinds.get(hit["kind"])
    if ver:
        hit = {**hit, "ver": ver, "target": k.render_target(ver, **{g: v for g, v in hit["groups"].items()
                                                                     if g != "ver"})}
    rec = {"source": "local", "key": f"local:{filename}", "name": filename, "kind": k.name,
           "version": hit["ver"], "target": hit["target"], "path": path, "size": size,
           "family": hit["groups"].get("model"), "tag": k.tag_for(hit["target"]), "sha1": None, "md5": None}
    rec.update(extra or {})
    return rec


def discover(cfg, kinds):
    """Scan vendor drop dirs (depth 1, unreadable paths skipped) and merge with registered entries."""
    recs, seen = {}, set()
    for key, e in load_registry(cfg).items():
        if os.path.exists(e["path"]):
            e = dict(e)
            e["registered"] = True
            recs[key] = e
            seen.add(os.path.realpath(e["path"]))
        else:
            e = dict(e)
            e["registered"] = True
            e["missing_file"] = True
            recs[key] = e
    for d in cfg.images_dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            p = os.path.join(d, fn)
            if not os.path.isfile(p) or os.path.realpath(p) in seen:
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            rec = local_record(kinds, fn, p, size=size, extra={"registered": False})
            if rec:
                cur = recs.get(rec["key"])
                if cur is None:
                    recs[rec["key"]] = rec
                elif cur.get("missing_file"):    # registered file was moved into an images dir: follow it
                    recs[rec["key"]] = {**cur, "path": p, "size": size, "missing_file": False}
                    recs[rec["key"]].pop("missing_file")
    return list(recs.values())


IMAGE_EXT = (".qcow2", ".vmdk", ".tgz", ".tar.gz", ".bin", ".iol")


def scan_files(cfg, kinds):
    """Image files (IMAGE_EXT, depth 1) in the images dirs: [{path, filename, size, rec}] with rec the detected
    local record (kind/version/predicted tag) or None. Unreadable dirs/files are skipped."""
    out = []
    for d in cfg.images_dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            p = os.path.join(d, fn)
            if not fn.lower().endswith(IMAGE_EXT) or not os.path.isfile(p):
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            out.append({"path": p, "filename": fn, "size": size,
                        "rec": local_record(kinds, fn, p, size=size, extra={"registered": False})})
    return out


def hashed(rec):
    """Fill size/md5/sha1 of a local record from its file."""
    h = hash_file(rec["path"])
    rec["size"] = os.path.getsize(rec["path"])
    rec["md5"], rec["sha1"] = h["md5"], h["sha1"]
    return rec


def append_result(cfg, rec):
    os.makedirs(os.path.dirname(cfg.results_file), exist_ok=True)
    with open(cfg.results_file, "a") as f:
        f.write(json.dumps(rec) + "\n")


def last_results(cfg):
    last = {}
    try:
        with open(cfg.results_file) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                last[r.get("image")] = r
    except OSError:
        pass
    return last
