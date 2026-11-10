"""Inventory: join kinds + LabHub index + local registry + docker images; ref resolution; status."""

import os
import re

from . import dockerx, index as ix, registry
from .util import ClimgError, version_key


class Inventory:
    def __init__(self, cfg, kinds, refresh=False, need_index=True, quiet=False):
        self.cfg, self.kinds = cfg, kinds
        self.idx = self.meta = None
        self.labhub = []
        if need_index:
            try:
                self.idx, self.meta = ix.load_index(cfg, refresh=refresh, quiet=quiet)
                self.labhub = ix.build_records(self.idx, kinds)
            except ClimgError as ex:
                if refresh:
                    raise
                print(f"warning: LabHub index unavailable ({ex}); continuing without it")
        self.local = registry.discover(cfg, kinds)
        self.builds = registry.load_builds(cfg)
        try:
            self.images = dockerx.list_images()
        except ClimgError as ex:
            print(f"warning: {ex}; image status is unknown")
            self.images = []
        self.tags = dockerx.tag_map(self.images)

    # ------------------------------------------------------------------ status
    def built_match(self, rec):
        """(tag, image) of the built image serving this record, else None. Checks the predicted tag, tags added by
        `climg add --tag`, and for family kinds (huawei) any `<repo>:<family>-*` tag."""
        k = self.kinds.get(rec["kind"]) if rec.get("kind") else None
        if not k or not rec.get("tag"):
            return None
        for t in [rec["tag"]] + list(rec.get("extra_tags") or []):
            if t in self.tags:
                return t, self.tags[t]
        if k.family_match and rec.get("family"):
            prefix = f"{k.repo}:{rec['family']}-"
            for t, im in sorted(self.tags.items()):
                if t.startswith(prefix):
                    return t, im
        return None

    def is_built(self, rec):
        m = self.built_match(rec)
        return m[1] if m else None

    def built_tag(self, rec):
        m = self.built_match(rec)
        return m[0] if m else rec.get("tag")

    def cached_source(self, rec):
        """Path of the already-downloaded LabHub source file (own downloads or read-only extra caches)."""
        f = rec.get("file")
        if not f:
            return None
        md5 = f["checksum"]["md5"]
        for d in [self.cfg.downloads_dir] + self.cfg.extra_cache_dirs:
            p = os.path.join(d, md5, f["filename"])
            if os.path.isfile(p):
                return p
        return None

    def status(self, rec):
        """(status, detail): built | cached | available | unbuildable | missing-file."""
        if not rec.get("kind"):
            return "unbuildable", ""
        im = self.is_built(rec)
        if im:
            return "built", self.built_tag(rec)
        if rec["source"] == "local":
            return ("cached", rec["path"]) if os.path.exists(rec["path"]) else ("missing-file", rec["path"])
        p = self.cached_source(rec)
        return ("cached", p) if p else ("available", "")

    def all_records(self, include_unbuildable=False):
        recs = [r for r in self.labhub if include_unbuildable or r["kind"]] + list(self.local)
        return recs

    # ------------------------------------------------------------------ refs
    def _by_tag(self, tag):
        return [r for r in self.all_records() if r.get("tag") == tag]

    def _pick(self, recs):
        """built > cached > available; newest version first; LabHub before local on ties."""
        order = {"built": 0, "cached": 1, "available": 2}
        return sorted(recs, key=lambda r: (order.get(self.status(r)[0], 3), _neg_version(r.get("version")),
                                           r["source"] != "labhub"))[0]

    def resolve(self, ref):
        """Resolve a user reference to a record. Raises ClimgError with candidates when unknown/ambiguous."""
        ref = ref.strip()
        if ref.startswith("#"):
            if not ref[1:].isdigit():
                raise ClimgError(f"bad id reference {ref!r}", "invalid_ref")
            n = int(ref[1:])
            hits = [r for r in self.labhub if r["id"] == n and r["section"] == "QEMU"]
            if not hits:
                hits = [r for r in self.labhub if r["id"] == n]
            if not hits:
                raise ClimgError(f"no LabHub entry with id {n} in the current index", "not_found")
            return hits[0]
        if ref.startswith("local:"):
            name = ref[6:]
            hits = [r for r in self.local if r["name"] == name]
            if not hits:
                raise ClimgError(f"no local image {name!r}; see `climg search --source local`", "not_found")
            return hits[0]
        if ref.startswith("labhub:"):
            body = ref[7:]
            name, _, sha = body.partition("@")
            hits = [r for r in self.labhub if r["name"] == name]
            if sha:
                hits = [r for r in hits if (r["files"][0]["checksum"]["sha1"] if r["files"] else "").startswith(sha)]
            if not hits:
                raise ClimgError(f"no LabHub entry {body!r} in the current index", "not_found")
            if len(hits) > 1:
                raise ClimgError(f"{name!r} is duplicated in the index; add @<sha1 prefix>: "
                                 + ", ".join(r["key"] for r in hits), "invalid_ref")
            return hits[0]
        by_tag = self._by_tag(ref)
        if by_tag:
            return self._pick(by_tag)
        if ref in self.tags:  # built image that no record predicts
            return self.docker_record(ref)
        k = self.kinds.find(ref)
        if k:
            recs = [r for r in self.all_records() if r["kind"] == k.name]
            if not recs:
                raise ClimgError(f"kind {k.name} has no known image (index/local)", "not_found")
            return self._pick(recs)
        # bare LabHub / local name or "kind version"
        hits = [r for r in self.all_records() if r["name"] == ref]
        if len(hits) == 1:
            return hits[0]
        words = ref.split()
        if len(words) == 2 and self.kinds.find(words[0]):
            k = self.kinds.find(words[0])
            hits = [r for r in self.all_records() if r["kind"] == k.name and r["version"] == words[1]]
            if hits:
                return self._pick(hits)
        raise ClimgError(f"cannot resolve {ref!r}: use labhub:<name>, local:<name>, #<id>, a kind alias "
                         f"({', '.join(sorted({k.alias for k in self.kinds}))}) or a docker tag", "not_found")

    def docker_record(self, tag):
        im = self.tags[tag]
        repo, ver = dockerx.split_tag(tag)
        k = self.kinds.by_repo(repo)
        return {"source": "docker", "key": f"docker:{tag}", "name": tag, "kind": k.name if k else None,
                "version": ver, "target": None, "tag": tag, "size": im["size"], "files": [], "file": None,
                "sha1": None, "family": None}

    # ------------------------------------------------------------------ images
    def source_of(self, tag):
        """Where an existing image came from: ('climg', key) | ('labhub'|'local', key, inferred) | ('external', None)."""
        b = self.builds.get(tag)
        if b:
            return "climg", b.get("key")
        for r in self.all_records():
            if r.get("tag") == tag:
                return r["source"], r["key"]
        return "external", None


def _neg_version(v):
    """Sort key putting the highest version first."""
    return [(-a, _InvStr(b) if isinstance(b, str) else -b) for a, b in version_key(v)]


class _InvStr(str):
    def __lt__(self, other):
        return str.__gt__(self, other)

    def __gt__(self, other):
        return str.__lt__(self, other)
