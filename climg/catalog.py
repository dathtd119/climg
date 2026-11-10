"""kinds.yaml: kind definitions, LabHub name -> kind inference, vendor filename detection."""

import os
import re

import yaml

from .util import ClimgError

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KINDS_FILE = os.path.join(ROOT_DIR, "kinds.yaml")
PATCHES_DIR = os.path.join(ROOT_DIR, "patches")


class Kind:
    def __init__(self, name, d):
        self.name = name
        self.d = d
        self.vendor = d["vendor"]
        self.dir = d["dir"]
        self.vendor_common = d.get("vendor_common") or os.path.join(os.path.dirname(d["dir"]), "common")
        self.repo = d["repo"]
        self.aliases = list(d.get("aliases") or [])
        self.image_ext = ["." + e.lstrip(".") for e in d["image_ext"]]
        self.target = d["target"]
        self.family_match = bool(d.get("family_match"))
        self.make = d["make"]
        self.patches = list(d.get("patches") or [])
        for pn in self.patches:
            if not isinstance(pn, str) or not re.fullmatch(r"[A-Za-z0-9][\w.+-]*", pn):
                raise ClimgError(f"{name}: bad patch name {pn!r} (a plain file name inside patches/)")
        self.qemu_env = d["qemu_env"]
        self.resources = d["resources"]
        self.env = dict(d.get("env") or {})
        self.boot_budget_s = int(d.get("boot_budget_s", 900))
        self.ssh_prompt = d["ssh_prompt"]
        self.probe = d["probe"]
        self.ports = d["ports"]
        self.ui = d.get("ui") or {}
        self.confirmed = d["confirmed"]
        self.example = d.get("example")
        self._version_rules = [(re.compile(r["re"]), r["fmt"]) for r in d["version_rules"]]
        self._labhub = [self._rule(r) for r in d.get("labhub") or []]
        self._vendor = [self._rule(r) for r in d.get("vendor_files") or []]

    @staticmethod
    def _rule(r):
        flags = re.I if "i" in str(r.get("flags", "")) else 0
        return {"re": re.compile(r["re"], flags), "ver": r.get("ver"), "target": r.get("target"),
                "section": r.get("section", "QEMU")}

    @property
    def alias(self):
        return self.aliases[0] if self.aliases else self.name

    @property
    def human_name(self):
        return f"{self.vendor.capitalize()} {self.name.split('_', 1)[1]}"

    def _apply(self, rule, text, extra=None):
        m = rule["re"].search(text)
        if not m:
            return None
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        groups.update(extra or {})
        ver = rule["ver"].format(**groups) if rule["ver"] else groups.get("ver")
        if not ver:
            return None
        groups["ver"] = ver
        return {"kind": self.name, "ver": ver, "target": (rule["target"] or self.target).format(**groups),
                "groups": groups}

    def match_labhub(self, name, section="QEMU", entry_id=None):
        for r in self._labhub:
            if r["section"] == section:
                hit = self._apply(r, name, {"id": entry_id})
                if hit:
                    return hit
        return None

    def match_vendor_file(self, filename):
        for r in self._vendor:
            hit = self._apply(r, filename)
            if hit:
                return hit
        return None

    def render_target(self, ver, **groups):
        return self.target.format(ver=ver, **groups)

    def predict_version(self, target):
        """Mirror of the platform Makefile VERSION sed (prediction only; make version-test is authoritative)."""
        for rx, fmt in self._version_rules:
            m = rx.fullmatch(target)
            if m:
                return m.expand(fmt)
        return None

    def tag_for(self, target):
        v = self.predict_version(target)
        return f"{self.repo}:{v}" if v else None


class Kinds:
    def __init__(self, path=None):
        path = path or KINDS_FILE
        with open(path) as f:
            raw = yaml.safe_load(f)
        base = raw["defaults"]
        self.kinds = {}
        for name, d in raw["kinds"].items():
            merged = {**base, **d}
            self.kinds[name] = Kind(name, merged)
        self.by_alias = {}
        for k in self.kinds.values():
            for a in k.aliases + [k.name]:
                self.by_alias[a.lower()] = k

    def __iter__(self):
        return iter(self.kinds.values())

    def get(self, name):
        return self.kinds.get(name)

    def find(self, word):
        return self.by_alias.get((word or "").lower())

    def by_repo(self, repo):
        """Kind for a docker repository (`vrnetlab/cisco_vios`, also with a registry prefix)."""
        tail = repo.split("vrnetlab/", 1)[-1] if "vrnetlab/" in repo else None
        for k in self:
            if repo == k.repo or (tail and k.repo == "vrnetlab/" + tail):
                return k
        return None

    def infer_labhub(self, name, section="QEMU", entry_id=None, filenames=()):
        """Kind inference for a LabHub entry: name rules first, then vendor-file rules on its disk files."""
        for k in self:
            hit = k.match_labhub(name, section, entry_id)
            if hit:
                return hit
        for fn in filenames:
            for k in self:
                if section == "QEMU" or section == "IOL":
                    hit = k.match_vendor_file(fn)
                    if hit and os.path.splitext(fn)[1] in k.image_ext:
                        return hit
        return None

    def detect_vendor_file(self, filename, kind=None):
        """Kind/version/target for a vendor-supplied file name; None when no rule matches."""
        for k in ([self.find(kind)] if kind else list(self)):
            if kind and k is None:
                raise ClimgError(f"unknown kind '{kind}'. known: {', '.join(self.kinds)}")
            hit = k.match_vendor_file(os.path.basename(filename))
            if hit:
                return hit
        return None


_cache = {}


def load_kinds(path=None):
    path = path or KINDS_FILE
    if path not in _cache:
        _cache[path] = Kinds(path)
    return _cache[path]
