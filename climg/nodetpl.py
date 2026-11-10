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

def load_owned(cfg):
    try:
        with open(cfg.nodetpl_owned_file) as f:
            return set(json.load(f).get("names", []))
    except (OSError, ValueError):
        return set()


def save_owned(cfg, names):
    os.makedirs(os.path.dirname(cfg.nodetpl_owned_file), exist_ok=True)
    tmp = cfg.nodetpl_owned_file + ".new"
    with open(tmp, "w") as f:
        json.dump({"names": sorted(names)}, f, indent=1)
    os.replace(tmp, cfg.nodetpl_owned_file)


# ------------------------------------------------------------------ pure merge

def merge(current, ops, owned):
    """Apply ops to the template list touching only owned names.
    ops: [("upsert", entry) | ("delete", name)].
    Returns (new_list, changes, skipped): changes = [(action, name)] with action created|updated|deleted,
    skipped = [(name, reason)]. Unowned entries are never modified or removed."""
    new = [dict(c) for c in current]
    changes, skipped = [], []
    for op, arg in ops:
        name = arg["name"] if op == "upsert" else arg
        idx = next((i for i, c in enumerate(new) if c.get("name") == name), None)
        if op == "upsert":
            if idx is None:
                new.append(dict(arg))
                changes.append(("created", name))
            elif name not in owned:
                skipped.append((name, "exists and is not owned by climg"))
            else:
                upd = {**arg, "setDefault": bool(new[idx].get("setDefault", False))}
                if upd != new[idx]:
                    new[idx] = upd
                    changes.append(("updated", name))
        else:
            if idx is None:
                continue
            if name not in owned:
                skipped.append((name, "not owned by climg"))
            else:
                del new[idx]
                changes.append(("deleted", name))
    return new, changes, skipped


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
        for action, name in changes:
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


def apply_ops(cfg, ops, writer=None, dry_run=False):
    """Locked read-merge-write of the given ops; returns (changes, skipped, writer name)."""
    w = writer or pick_writer(cfg)
    with locked(cfg):
        owned = load_owned(cfg)
        current = w.read()
        if current is None:
            current = w.initial()
        new, changes, skipped = merge(current, ops, owned)
        if changes and not dry_run:
            w.commit(new, changes)
            for action, name in changes:
                (owned.discard if action == "deleted" else owned.add)(name)
            save_owned(cfg, owned)
        elif not dry_run:
            # owned names that were removed by someone else are forgotten so a later user entry is never claimed
            present = {c.get("name") for c in current}
            if owned - present:
                save_owned(cfg, owned & present)
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
    ops += [("delete", n) for n in sorted(load_owned(cfg) - set(want))]
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
        changes, skipped, via = apply_ops(cfg, [("upsert", e)])
        if changes:
            print(f"node template {e['name']}: {changes[0][0]} (via {via})")
        elif not skipped:
            print(f"node template {e['name']}: up to date")
        return {"name": e["name"], "written": bool(changes), "writer": via,
                "action": changes[0][0] if changes else ("skipped" if skipped else "unchanged"), "entry": e}
    except ClimgError as ex:
        warn(f"node template for {tag} not written: {ex}; run `climg nodetpl sync` later")
        return {"name": e["name"], "written": False, "entry": e, "error": str(ex)}
