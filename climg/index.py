"""LabHub index: cached fetch with failover, URL building, normalised image records."""

import json
import os
import re
import time
import urllib.parse
import urllib.request

from .util import UA, ClimgError, now

RELEASE_API = "https://api.github.com/repos/ishare2-org/mirrors/releases/latest"
ASSET = "labhub.json"
ARCHIVE_EXT = (".tgz", ".tar.gz", ".zip")


def _http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _http_bytes(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def read_meta(cfg):
    try:
        with open(cfg.index_meta) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def validate_index(data):
    if not isinstance(data, dict) or "url_properties" not in data or "QEMU" not in data:
        raise ClimgError("not a LabHub index (missing url_properties/QEMU)")
    return data


def refresh_index(cfg, log=print):
    """Download the latest release asset; keep the old copy if anything fails. Returns meta."""
    os.makedirs(os.path.dirname(cfg.index_file), exist_ok=True)
    errors = []
    try:
        rel = _http_json(RELEASE_API)
        asset = next(a for a in rel["assets"] if a["name"] == ASSET)
        data = _http_bytes(asset["browser_download_url"])
        validate_index(json.loads(data))
        meta = {"release": rel["tag_name"], "fetched_at": time.time(), "source": asset["browser_download_url"]}
    except Exception as ex:
        errors.append(f"github release: {ex!r}")
        data = meta = None
        for p in cfg.index_fallback_files:
            try:
                with open(p, "rb") as f:
                    data = f.read()
                validate_index(json.loads(data))
                meta = {"release": cfg.pinned_release + " (fallback copy)", "fetched_at": time.time(), "source": p}
                log(f"warning: GitHub unreachable ({errors[0]}); using fallback copy {p}")
                break
            except Exception as ex2:
                errors.append(f"{p}: {ex2!r}")
    if not data:
        raise ClimgError("cannot fetch LabHub index: " + "; ".join(errors))
    tmp = cfg.index_file + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, cfg.index_file)
    with open(cfg.index_meta, "w") as f:
        json.dump(meta, f)
    return meta


def load_index(cfg, refresh=False, quiet=False):
    """Return (index dict, meta). Refreshes when missing/stale/forced; falls back to the stale copy on failure."""
    meta = read_meta(cfg)
    have = os.path.exists(cfg.index_file)
    stale = not have or (time.time() - meta.get("fetched_at", 0)) > cfg.index_ttl
    if refresh or stale:
        try:
            meta = refresh_index(cfg, log=(lambda m: None) if quiet else print)
        except ClimgError:
            if refresh or not have:
                raise
            if not quiet:
                print("warning: index refresh failed; using the cached copy")
    with open(cfg.index_file) as f:
        return validate_index(json.load(f)), meta


def mirror_urls(idx, path, order):
    """URLs for a LabHub path in the configured mirror order (`secondary` redirects: only if listed)."""
    up = idx["url_properties"]
    out = []
    for key in order:
        host = up["hostnames"].get(key)
        if host:
            out.append(f"{up['protocol']}://{host}{up['prefixes'].get(key, '')}{path}")
    return out


def pick_source_file(entry, kind):
    """Choose the file of an entry that carries the disk image: (file, 'plain'|'archive') or (None, None)."""
    files = entry["files"]
    plain = [f for f in files if f["extension"].lower() in kind.image_ext]
    if plain:
        return max(plain, key=lambda f: f["size"]), "plain"
    arch = [f for f in files if f["filename"].lower().endswith(ARCHIVE_EXT)]
    if arch:
        return max(arch, key=lambda f: f["size"]), "archive"
    return None, None


def entry_location(section, entry):
    """(folder, model) of a LabHub entry from its first file path: `/addons/qemu/H3C/vSR/x.tgz` -> (`H3C/vSR`, `vSR`).
    QEMU folders are relative to `qemu`; IOL/DYNAMIPS keep the section folder (`iol/bin`, `dynamips`) and have no model."""
    if not entry["files"]:
        return None, None
    parts = [p for p in urllib.parse.unquote(entry["files"][0]["path"]).split("/") if p][1:-1]   # drop `addons`, file
    if section == "QEMU" and parts and parts[0].lower() == "qemu":
        parts = parts[1:]
        return ("/".join(parts) or None), (parts[1] if len(parts) > 1 else None)
    return ("/".join(parts) or None), None


def build_records(idx, kinds):
    """Normalised records for every entry of every section (QEMU, IOL, DYNAMIPS...). kind is None when nothing
    buildable matches; vendor/folder/model are always set (see vendors.yaml)."""
    recs = []
    for section in ("QEMU", "IOL", "DYNAMIPS"):
        for e in idx.get(section, []):
            fnames = [f["filename"] for f in e["files"]]
            hit = kinds.infer_labhub(e["name"], section, e["id"], fnames)
            rec = {"source": "labhub", "section": section, "id": e["id"], "name": e["name"],
                   "kind": hit["kind"] if hit else None, "version": hit["ver"] if hit else None,
                   "target": hit["target"] if hit else None, "files": e["files"],
                   "size": e["metadata"].get("total_size"), "sha1": None, "file": None, "file_mode": None,
                   "family": hit["groups"].get("model") if hit else None, "tag": None}
            rec["folder"], rec["model"] = entry_location(section, e)
            if hit:
                k = kinds.get(hit["kind"])
                f, mode = pick_source_file(e, k)
                rec["file"], rec["file_mode"] = f, mode
                if f:
                    rec["size"] = f["size"]
                    rec["sha1"] = f["checksum"]["sha1"]
                rec["tag"] = k.tag_for(hit["target"])
                if f is None:
                    rec["kind"] = None   # no disk file we can use
            rec["vendor"] = kinds.vendor_of(rec["kind"], rec["folder"], e["name"])
            recs.append(rec)
    # stable key: labhub:<name>, disambiguated by sha1 prefix when the name is duplicated
    seen = {}
    for r in recs:
        seen.setdefault(r["name"], []).append(r)
    for name, group in seen.items():
        for r in group:
            if len(group) > 1:
                sha = (r["files"][0]["checksum"]["sha1"] if r["files"] else "")[:8]
                r["key"] = f"labhub:{name}@{sha}"
            else:
                r["key"] = f"labhub:{name}"
    return recs


def index_age(meta):
    t = meta.get("fetched_at")
    if not t:
        return None
    s = int(time.time() - t)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def safe_filename(name):
    return urllib.parse.unquote(name.rsplit("/", 1)[-1])


def is_match(text, kw):
    return all(w in text.lower() for w in kw.lower().split())
