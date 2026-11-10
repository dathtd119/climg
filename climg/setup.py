"""`climg init`: detect workspace / app container / Node Template file, write the config, create directories."""

import json
import os
import subprocess

import yaml

from .config import DEFAULTS
from .util import ClimgError

DEFAULT_WORKSPACE = "~/.clab"
APP_IMAGE_MARK = "containerlab-web"
DEFAULT_APP = "containerlab-app"


def _docker(args):
    try:
        p = subprocess.run(["docker"] + args, text=True, capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def running_containers():
    out = _docker(["ps", "--format", "{{.Names}}\t{{.Image}}"])
    return [ln.split("\t", 1) for ln in (out or "").splitlines() if "\t" in ln]


def find_app(configured=None, containers=None):
    """Running app container: configured name, else `containerlab-app`, else any container whose image
    contains containerlab-web. Returns the name or None."""
    containers = running_containers() if containers is None else containers
    names = {n for n, _ in containers}
    for cand in (configured, DEFAULT_APP):
        if cand and cand in names:
            return cand
    return next((n for n, img in containers if APP_IMAGE_MARK in img), None)


def inspect_app(name):
    """(WEB_LOCAL_WORKSPACE or None, host source of the /root/.clab mount or None)."""
    out = _docker(["inspect", name, "--format", "{{json .Config.Env}}\t{{json .Mounts}}"])
    if not out:
        return None, None
    env_s, mounts_s = out.strip().split("\t", 1)
    ws = next((e.split("=", 1)[1] for e in json.loads(env_s) if e.startswith("WEB_LOCAL_WORKSPACE=")), None)
    src = next((m["Source"] for m in json.loads(mounts_s) if m.get("Destination") == "/root/.clab"), None)
    return ws or None, src


def detect(flags, existing=None, containers=None, inspector=inspect_app):
    """Pure-ish detection. flags: workspace, images_dir, vrnetlab, app_container (None when not given).
    Returns the dict of config values to write plus a `notes` list explaining each choice."""
    existing = existing or {}
    notes = []
    app = find_app(flags.get("app_container") or existing.get("app_container"), containers)
    app_ws = app_src = None
    if app:
        app_ws, app_src = inspector(app)
    if flags.get("workspace"):
        ws, why = flags["workspace"], "--workspace"
    elif app:
        ws, why = (app_ws, f"WEB_LOCAL_WORKSPACE of {app}") if app_ws else (DEFAULT_WORKSPACE, f"{app} sets no WEB_LOCAL_WORKSPACE")
    else:
        ws, why = DEFAULT_WORKSPACE, "containerlab default (no app container running)"
    ws = os.path.expanduser(ws)
    notes.append(f"workspace: {ws}  ({why})")
    if app:
        if app_src:
            tpl = os.path.join(app_src, "ui", "custom-nodes.json")
            notes.append(f"node templates: {tpl}  (host source of {app}:/root/.clab)")
        else:
            tpl = None
            notes.append(f"node templates: REST only ({app} does not mount /root/.clab)")
    else:
        tpl = os.path.join(ws, "ui", "custom-nodes.json")
        notes.append(f"node templates: {tpl}  (no app container)")
    images = flags.get("images_dir")
    imgs = [os.path.expanduser(images)] if images else existing.get("images_dirs") or ["~/.local/share/climg/images"]
    imgs = [os.path.expanduser(p) for p in imgs]
    vr = flags.get("vrnetlab") or existing.get("vrnetlab")
    if not vr and os.path.isdir(os.path.expanduser("~/containerlab/vrnetlab")):
        vr = "~/containerlab/vrnetlab"
    labs = [ws if os.path.realpath(os.path.expanduser(p)) == os.path.realpath(ws) else p
            for p in existing.get("lab_roots") or []] or [ws]
    cfg = {k: v for k, v in existing.items() if k in DEFAULTS and k != "vendor_dirs"}
    cfg.update({"workspace": ws, "images_dirs": imgs, "vrnetlab": vr, "app_container": app,
                "nodetpl_file": tpl, "lab_roots": labs})
    cfg.setdefault("state_dir", DEFAULTS["state_dir"])
    cfg.setdefault("credentials", DEFAULTS["credentials"])
    notes.append(f"images dirs: {', '.join(imgs)}")
    notes.append(f"vrnetlab: {vr or '(not found; set --vrnetlab)'}")
    notes.append(f"app container: {app or '-'}")
    notes.append(f"lab roots: {', '.join(labs)}")
    return cfg, notes


def read_existing(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ClimgError(f"{path}: expected a mapping")
    if "vendor_dirs" in data:      # replaced by images_dirs (hand-imported vendor images)
        data.pop("vendor_dirs")
    return data


def write_config(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".new"
    with open(tmp, "w") as f:
        yaml.safe_dump({k: v for k, v in cfg.items() if v is not None}, f, sort_keys=False)
    os.replace(tmp, path)


def make_dirs(cfg):
    """Create climg's own dirs and the workspace; returns the created paths."""
    made = []
    for d in cfg["images_dirs"] + [os.path.expanduser(cfg["state_dir"]), cfg["workspace"]]:
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            made.append(d)
    return made
