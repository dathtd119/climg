"""Small shared helpers: sizes, versions, subprocess, logging, hashing."""

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys

UA = "climg/0.1 (+containerlab image manager)"  # Cloudflare answers 403 to Python's default User-Agent


class ClimgError(Exception):
    """Expected failure: printed without traceback, exit status 1."""


def now():
    return dt.datetime.now().astimezone()


def gb(n):
    return f"{n / 1e9:.1f} GB"


def human(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def free_bytes(path):
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    st = os.statvfs(path or "/")
    return st.f_bavail * st.f_frsize


def version_key(v):
    """Natural sort key: digit runs compare as numbers."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.findall(r"\d+|[^\d]+", v or "")]


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def table(rows, headers):
    """Plain-text table; rows are sequences of str."""
    cols = list(zip(*([headers] + [[str(c) for c in r] for r in rows])))
    widths = [max(len(c) for c in col) for col in cols]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for r in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(out)


def emit_json(obj):
    print(json.dumps(obj, indent=2, sort_keys=False, default=str))


def run(cmd, log=None, cwd=None, env=None, check=False, timeout=None):
    """Run a command capturing stdout+stderr; mirrors output into the log when given."""
    if log:
        log(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""), echo=False)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError as ex:
        raise ClimgError(f"command not found: {cmd[0]} ({ex})")
    if log and p.stdout:
        for ln in p.stdout.rstrip().splitlines():
            log.fh.write("    " + ln + "\n")
    if check and p.returncode != 0:
        raise ClimgError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{(p.stdout or '')[-2000:]}")
    return p


def hash_file(path, algos=("md5", "sha1"), block=1 << 22):
    hs = {a: hashlib.new(a) for a in algos}
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(block), b""):
            for h in hs.values():
                h.update(blk)
    return {a: h.hexdigest() for a, h in hs.items()}


class Log:
    """Append-only per-name log that can also echo to stdout."""

    def __init__(self, cfg, name):
        os.makedirs(cfg.logs_dir, exist_ok=True)
        safe = re.sub(r"[^\w.@-]+", "_", name)
        self.path = os.path.join(cfg.logs_dir, f"{safe}.log")
        self.fh = open(self.path, "a", buffering=1)

    def __call__(self, msg, echo=True):
        self.fh.write(f"{now().isoformat(timespec='seconds')} {msg}\n")
        if echo:
            print(msg, flush=True)
