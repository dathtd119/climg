"""Per-host configuration (~/.config/climg/config.yaml)."""

import os

import yaml

from .util import ClimgError

DEFAULTS = {
    "vrnetlab": None,                  # srl-labs/vrnetlab checkout (never modified; builds run in a work copy)
    "state_dir": "~/.local/share/climg",
    "extra_cache_dirs": [],            # read-only caches laid out as <dir>/<md5>/<filename>
    "images_dirs": [],                 # hand-imported vendor images (default: <state>/images)
    "workspace": None,                 # containerlab workspace (default ~/.clab, or the app's WEB_LOCAL_WORKSPACE)
    "app_container": None,             # running containerlab-app container, when one is used
    "nodetpl_file": None,              # host path of the app's ui/custom-nodes.json; null = REST only
    "nodetpl_url": "https://127.0.0.1:3001",   # containerlab-app base URL for REST mode
    "nodetpl_writer": "auto",          # auto | file | rest
    "mirrors": ["main", "drive", "legacy"],   # LabHub hostname keys, tried in this order
    "disk_floor_gb": 15,
    "max_builds": 2,
    "index_ttl_hours": 168,
    "index_pinned_release": "LabHub-Indexes-2.0.1",
    "index_fallback_files": [],        # local copies of labhub.json used when the network is down
    "credentials": None,               # {username, password}: required by template/test (owner decision)
    "lab_roots": [],                   # directories scanned by `climg lab` without arguments
}


def config_path():
    return os.environ.get("CLIMG_CONFIG") or os.path.expanduser("~/.config/climg/config.yaml")


class Config:
    def __init__(self, data=None, path=None):
        data = data or {}
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            raise ClimgError(f"unknown config keys in {path}: {', '.join(sorted(unknown))}")
        self.path = path
        self.raw = {**DEFAULTS, **data}
        x = os.path.expanduser
        self.vrnetlab = x(self.raw["vrnetlab"]) if self.raw["vrnetlab"] else None
        self.state = x(os.environ.get("CLIMG_STATE") or self.raw["state_dir"])
        self.extra_cache_dirs = [x(p) for p in self.raw["extra_cache_dirs"] or []]
        self.images_dirs = [x(p) for p in self.raw["images_dirs"] or []] or [os.path.join(self.state, "images")]
        self.workspace = x(self.raw["workspace"] or "~/.clab")
        self.app_container = self.raw["app_container"]
        self.nodetpl_file = x(self.raw["nodetpl_file"]) if self.raw["nodetpl_file"] else None
        self.nodetpl_url = self.raw["nodetpl_url"]
        self.nodetpl_writer = self.raw["nodetpl_writer"]
        if self.nodetpl_writer not in ("auto", "file", "rest"):
            raise ClimgError(f"{path}: nodetpl_writer must be auto, file or rest")
        self.exists = bool(path and os.path.exists(path))
        self.mirrors = list(self.raw["mirrors"])
        self.disk_floor = int(float(self.raw["disk_floor_gb"]) * 1e9)
        self.max_builds = int(self.raw["max_builds"])
        self.index_ttl = float(self.raw["index_ttl_hours"]) * 3600
        self.pinned_release = self.raw["index_pinned_release"]
        self.index_fallback_files = [x(p) for p in self.raw["index_fallback_files"] or []]
        self.credentials = self.raw["credentials"]
        self.lab_roots = [x(p) for p in self.raw["lab_roots"] or []]

    # state layout
    @property
    def index_file(self):
        return os.path.join(self.state, "index", "labhub.json")

    @property
    def index_meta(self):
        return os.path.join(self.state, "index", "meta.json")

    downloads_dir = property(lambda s: os.path.join(s.state, "downloads"))
    work_dir = property(lambda s: os.path.join(s.state, "work"))
    locks_dir = property(lambda s: os.path.join(s.state, "locks"))
    logs_dir = property(lambda s: os.path.join(s.state, "logs"))
    runs_dir = property(lambda s: os.path.join(s.state, "runs"))
    results_file = property(lambda s: os.path.join(s.state, "results.jsonl"))
    registry_file = property(lambda s: os.path.join(s.state, "registry.json"))
    builds_file = property(lambda s: os.path.join(s.state, "builds.json"))
    nodetpl_owned_file = property(lambda s: os.path.join(s.state, "nodetpl-owned.json"))
    nodetpl_lock_file = property(lambda s: os.path.join(s.locks_dir, "nodetpl.lock"))

    def need_credentials(self):
        c = self.credentials
        if not (isinstance(c, dict) and c.get("username") and c.get("password")):
            raise ClimgError(f"credentials {{username, password}} are not set in {self.path or config_path()}")
        return c


def load_config(path=None):
    path = path or config_path()
    data = {}
    if os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ClimgError(f"{path}: expected a mapping")
    return Config(data, path)
