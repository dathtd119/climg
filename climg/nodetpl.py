"""Node Templates of the containerlab-app web UI (<home>/.clab/ui/custom-nodes.json).

climg only ever touches entries it created itself (names listed in nodetpl-owned.json). Two writers:
  file  - read-modify-write of the JSON file under a host flock; root-owned files go through `docker exec -i APP`
          (temp file + mv inside the container)
  rest  - POST/DELETE per entry on the app's /api/runtime/ui/custom-nodes
"""

import contextlib
import fcntl
import http.cookiejar
import json
import os
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import dockerx, template
from .util import ClimgError, progress, warn

APP_ROOT = "/root/.clab"                 # where the app container sees its workspace (mount destination)
APP_FILE = APP_ROOT + "/ui/custom-nodes.json"
BUILTIN_DEFAULTS = [   # what the app serves while the file is missing; a file created by us must start with these
    {"name": "SRLinux Latest", "kind": "nokia_srlinux", "type": "ixr-d1", "image": "ghcr.io/nokia/srlinux:latest",
     "icon": "router", "baseName": "srl", "interfacePattern": "e1-{n}", "setDefault": True},
    {"name": "Network Multitool", "kind": "linux", "image": "ghcr.io/srl-labs/network-multitool:latest",
     "icon": "client", "baseName": "client", "interfacePattern": "eth{n}", "setDefault": False},
]


# ------------------------------------------------------------------ naming / entries

def tpl_name(kind, tag):
    """<vendor>-<model>-<version>: lower-case except the version. Family kinds (huawei) carry the model in the tag."""
    ver = tag.rsplit(":", 1)[1]
    if kind.family_match:
        return f"{kind.vendor}-{ver}".lower()
    model = kind.ui.get("model") or kind.name.split("_", 1)[1]
    return f"{kind.vendor.lower()}-{model.lower()}-{ver}"


def build_entry(kind, tag):
    ver = tag.rsplit(":", 1)[1]
    base = ver.split("-", 1)[0].lower() if kind.family_match else kind.ui.get("base") or kind.alias.replace("_", "")
    e = {"name": tpl_name(kind, tag), "kind": kind.name, "image": tag, "icon": kind.ui.get("icon", "router"),
         "baseName": base, "interfacePattern": kind.ui.get("pattern", "eth{n}"), "setDefault": False}
    env = template.node_env(kind)
    if env:
        e["env"] = env
    return e


def entry_for_tag(kinds, tag):
    k = kinds.by_repo(dockerx.split_tag(tag)[0])
    return build_entry(k, tag) if k else None


# ------------------------------------------------------------------ ownership

def _load_owned_doc(cfg):
    try:
        with open(cfg.nodetpl_owned_file) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def load_owned(cfg):
    """Owned names; reads both {"names": [...]} (older climg) and the extended format."""
    names = _load_owned_doc(cfg).get("names", [])
    return set(names) if isinstance(names, list) else set()


def load_generated(cfg):
    """{name: last generated entry} (the baseline of the three-way merge); empty for older files."""
    gen = _load_owned_doc(cfg).get("generated", {})
    return {n: e for n, e in gen.items() if isinstance(e, dict)} if isinstance(gen, dict) else {}


def save_owned(cfg, names, generated=None):
    """Atomic write of {"names": [...], "generated": {name: entry}}; `generated` None keeps the stored baselines."""
    gen = load_generated(cfg) if generated is None else generated
    os.makedirs(os.path.dirname(cfg.nodetpl_owned_file), exist_ok=True)
    tmp = cfg.nodetpl_owned_file + ".new"
    with open(tmp, "w") as f:
        json.dump({"names": sorted(names), "generated": {n: gen[n] for n in sorted(gen) if n in names}}, f, indent=1)
    os.replace(tmp, cfg.nodetpl_owned_file)


def _strip(entry):
    """Entry without setDefault, which is always the user's."""
    return {k: v for k, v in entry.items() if k != "setDefault"}


def regen_for(kinds):
    """regen(entry) -> what climg would generate now for that template's image (None when unknown)."""
    def regen(entry):
        try:
            return entry_for_tag(kinds, entry.get("image") or "")
        except (ValueError, IndexError, AttributeError):
            return None
    return regen if kinds is not None else None


def is_customized(entry, base, regen=None):
    """True when the user changed a managed entry: differs from its stored base, or (no base) from the regenerated one."""
    ref = base if base is not None else (regen(entry) if regen else None)
    return ref is not None and _strip(entry) != _strip(ref)


def merge_entry(base, ours, theirs):
    """Field-level three-way merge. base None = unknown baseline (written by an older climg): ours equals theirs
    wherever it already does, every other field present in ours is a user edit. Returns (entry, kept)."""
    out, kept = {}, []
    for key in list(theirs) + [k for k in ours if k not in theirs]:
        if key == "setDefault":
            out[key] = bool(ours.get("setDefault", False))
            continue
        o, t = ours.get(key, _MISSING), theirs.get(key, _MISSING)
        if base is None:
            user = o != _MISSING and o != t       # no baseline: missing fields are filled, differing ones are edits
        else:
            user = o != base.get(key, _MISSING)
        take = o if user else t
        if take != _MISSING:
            out[key] = take
        if user and o != t:
            kept.append(key)
    return out, kept


_MISSING = object()


# ------------------------------------------------------------------ pure merge

def merge(current, ops, owned, base=None, regen=None):
    """Apply ops to the template list touching only owned names.
    ops: [("upsert", entry) | ("delete", name) | ("prune", name)]; prune = sync removal of a template whose image is
    gone, skipped when the user edited it; delete = explicit user removal, always done for owned names.
    base: {name: last generated entry}; regen(entry): what climg would generate now (for names without a base).
    Returns (new_list, changes, skipped): changes = [(action, name, kept)] with action created|updated|deleted and
    kept = fields whose user value was preserved; skipped = [(name, reason)]. Unowned entries are never touched."""
    base = base or {}
    new = [dict(c) for c in current]
    changes, skipped = [], []
    for op, arg in ops:
        name = arg["name"] if op == "upsert" else arg
        idx = next((i for i, c in enumerate(new) if c.get("name") == name), None)
        if op == "upsert":
            if idx is None:
                new.append(dict(arg))
                changes.append(("created", name, []))
            elif name not in owned:
                skipped.append((name, "exists and is not owned by climg"))
            else:
                upd, kept = merge_entry(base.get(name), new[idx], arg)
                if upd != new[idx]:
                    new[idx] = upd
                    changes.append(("updated", name, kept))
        else:
            if idx is None:
                continue
            if name not in owned:
                skipped.append((name, "not owned by climg"))
            elif op == "prune" and _edited(new[idx], base.get(name), regen):
                skipped.append((name, "edited by user; image missing"))
            else:
                del new[idx]
                changes.append(("deleted", name, []))
    return new, changes, skipped


def _edited(entry, base, regen):
    ref = base if base is not None else (regen(entry) if regen else None)
    return ref is None or _strip(entry) != _strip(ref)     # unknown reference: be conservative


# ------------------------------------------------------------------ writers

def _docker_cat(app, path):
    p = subprocess.run(["docker", "exec", app, "cat", path], text=True, capture_output=True)
    if p.returncode != 0:
        if "No such file" in p.stderr:
            return None
        raise ClimgError(f"docker exec {app} cat {path}: {p.stderr.strip()[:200]}")
    return p.stdout


class FileWriter:
    name = "file"

    def __init__(self, cfg):
        if not cfg.nodetpl_file:
            raise ClimgError("Node Template file unknown (REST only: /root/.clab is not mounted into the app, "
                             "or no app); use --writer rest or run `climg init`")
        self.cfg, self.path, self.app = cfg, cfg.nodetpl_file, cfg.app_container

    def read(self):
        """List of templates, or None when the file does not exist."""
        try:
            text = open(self.path).read()
        except FileNotFoundError:
            return None
        except PermissionError:
            if not self.app:
                raise ClimgError(f"{self.path} is not readable and no app container is configured")
            text = _docker_cat(self.app, APP_FILE)
            if text is None:
                return None
        try:
            data = json.loads(text)
        except ValueError as ex:
            raise ClimgError(f"{self.path}: invalid JSON ({ex})")
        if not isinstance(data, list):
            raise ClimgError(f"{self.path}: expected a JSON array")
        return data

    def initial(self):
        try:
            return Rest(self.cfg).read()     # exactly what the app serves while the file is missing
        except ClimgError:
            return [dict(d) for d in BUILTIN_DEFAULTS]

    def commit(self, new, changes):
        text = json.dumps(new, indent=2) + "\n"
        d = os.path.dirname(self.path)
        try:
            os.makedirs(d, mode=0o750, exist_ok=True)
            tmp = self.path + ".climg.tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
            return
        except PermissionError:
            with contextlib.suppress(OSError):
                os.unlink(self.path + ".climg.tmp")
        if not self.app:
            raise ClimgError(f"{self.path} is not writable and no app container is configured")
        tmp = APP_FILE + ".climg.tmp"
        script = f"mkdir -p {os.path.dirname(APP_FILE)} && cat > {tmp} && chmod 0640 {tmp} && mv {tmp} {APP_FILE}"
        p = subprocess.run(["docker", "exec", "-i", self.app, "sh", "-c", script], input=text, text=True,
                           capture_output=True)
        if p.returncode != 0:
            raise ClimgError(f"docker exec write failed: {p.stderr.strip()[:200]}")


class Rest:
    name = "rest"

    def __init__(self, cfg):
        self.base = cfg.nodetpl_url.rstrip("/")
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # the app's self-signed certificate
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                              urllib.request.HTTPSHandler(context=ctx))
        self._auth = False

    def _req(self, method, path, body=None):
        if not self._auth:
            self._auth = True
            self._req("GET", "/auth/endpoints")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.op.open(req, timeout=20) as r:
                raw = r.read()
        except urllib.error.HTTPError as ex:
            raise ClimgError(f"{method} {path}: HTTP {ex.code} {ex.read()[:200]!r}")
        except (urllib.error.URLError, OSError) as ex:
            raise ClimgError(f"containerlab-app REST at {self.base} unreachable: {ex}")
        return json.loads(raw) if raw.strip().startswith((b"{", b"[")) else None

    def read(self):
        d = self._req("GET", "/api/runtime/ui/custom-nodes")
        return list((d or {}).get("customNodes", []))

    initial = read

    def commit(self, new, changes):
        by = {c["name"]: c for c in new}
        for action, name, _ in changes:
            if action == "deleted":
                self._req("DELETE", "/api/runtime/ui/custom-nodes/" + urllib.parse.quote(name, safe=""))
            else:
                self._req("POST", "/api/runtime/ui/custom-nodes", by[name])


def reachable(cfg):
    try:
        Rest(cfg).read()
        return True
    except ClimgError:
        return False


def pick_writer(cfg, choice=None):
    choice = choice or cfg.nodetpl_writer
    if choice == "auto":
        choice = "rest" if reachable(cfg) else "file"
    return Rest(cfg) if choice == "rest" else FileWriter(cfg)


@contextlib.contextmanager
def locked(cfg):
    os.makedirs(os.path.dirname(cfg.nodetpl_lock_file), exist_ok=True)
    with open(cfg.nodetpl_lock_file, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def apply_ops(cfg, ops, writer=None, dry_run=False, kinds=None):
    """Locked read-merge-write of the given ops; returns (changes, skipped, writer name); changes are
    (action, name, kept) with kept = the fields whose user-edited value was preserved."""
    w = writer or pick_writer(cfg)
    with locked(cfg):
        owned = load_owned(cfg)
        base = load_generated(cfg)
        current = w.read()
        if current is None:
            current = w.initial()
        new, changes, skipped = merge(current, ops, owned, base, regen_for(kinds))
        if not dry_run:
            if changes:
                w.commit(new, changes)
            present = {c.get("name") for c in new}
            created = {n for a, n, _ in changes if a == "created"}
            gen, names = dict(base), set(owned)
            for op, arg in ops:                      # the new generated entry is the baseline of the next merge
                if op == "upsert" and (arg["name"] in owned or arg["name"] in created):
                    names.add(arg["name"])
                    gen[arg["name"]] = arg
            for action, name, _ in changes:
                if action == "deleted":
                    names.discard(name)
            # owned names that were removed by someone else are forgotten so a later user entry is never claimed
            names &= present
            if names != owned or gen != base:
                save_owned(cfg, names, gen)
    for name, why in skipped:
        warn(f"node template {name!r} skipped: {why}")
    return changes, skipped, w.name


def list_templates(cfg, writer=None):
    w = writer or pick_writer(cfg)
    cur = w.read()
    return (cur if cur is not None else w.initial()), load_owned(cfg), w.name


def built_entries(cfg, kinds, images=None):
    """One entry per tagged BUILT vrnetlab image of a known kind (also images climg did not build)."""
    images = dockerx.list_images() if images is None else images
    out = {}
    for im in images:
        if not im["vrnetlab"]:
            continue
        for tag in im["tags"]:
            e = entry_for_tag(kinds, tag)
            if e:
                out[e["name"]] = e
    return out


def sync_ops(cfg, kinds, images=None):
    """Upserts for every built image + deletes of owned templates whose image is gone."""
    want = built_entries(cfg, kinds, images)
    ops = [("upsert", e) for _, e in sorted(want.items())]
    ops += [("prune", n) for n in sorted(load_owned(cfg) - set(want))]
    return ops


def after_build(cfg, kinds, tag, write=True):
    """Hook for pull/add/build: upsert the template of a freshly built image; failures warn, never fail the build.
    Returns {name, written, ...} for the JSON result (None when the image has no known kind). With write=False
    (--no-nodetpl) nothing is written and the result names the template that would be."""
    e = entry_for_tag(kinds, tag)
    if not e:
        return None
    if not write:
        print(f"node template {e['name']}: not written (--no-nodetpl)")
        return {"name": e["name"], "written": False, "entry": e}
    progress("template", None, f"writing node template {e['name']}")
    try:
        changes, skipped, via = apply_ops(cfg, [("upsert", e)], kinds=kinds)
        if changes:
            kept = changes[0][2]
            print(f"node template {e['name']}: {changes[0][0]}" + (f" (kept your {', '.join(kept)})" if kept else "")
                  + f" (via {via})")
        elif not skipped:
            print(f"node template {e['name']}: up to date")
        res = {"name": e["name"], "written": bool(changes), "writer": via,
               "action": changes[0][0] if changes else ("skipped" if skipped else "unchanged"), "entry": e}
        if changes and changes[0][0] == "updated":
            res["kept"] = changes[0][2]
        return res
    except ClimgError as ex:
        warn(f"node template for {tag} not written: {ex}; run `climg nodetpl sync` later")
        return {"name": e["name"], "written": False, "entry": e, "error": str(ex)}
