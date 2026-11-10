"""Small shared helpers: sizes, versions, subprocess, logging, hashing."""

import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys

UA = "climg/0.1 (+containerlab image manager)"  # Cloudflare answers 403 to Python's default User-Agent


class ClimgError(Exception):
    """Expected failure: printed without traceback, exit status 1. `code` is the stable machine-readable error
    code reported in the --json error envelope."""

    def __init__(self, message="", code="error"):
        super().__init__(message)
        self.code = code


class NeedsYes(ClimgError):
    """A mutating command was run without --yes (exit status 3). `text` is what text mode prints on stdout."""

    def __init__(self, message, text=None):
        super().__init__(message, "needs_yes")
        self.text = text or message


class Cancelled(BaseException):
    """SIGTERM received: unwinds through every `finally` (cleanup) up to main(); exit status 4."""


# machine interface state (set by cli.main)
JSON_OUT = None        # real stdout while --json redirects sys.stdout to stderr
LOG_HOOK = None        # callable(line) feeding the job file log tail
PROGRESS_HOOK = None   # callable(phase, pct, message, state) feeding the job file
LAST_RESULT = None     # the result object of the command (what --json prints; the job file's final write)
SECRETS = []           # credential values that must never appear in JSON output, job files or log tails
REDACTED = "<redacted>"
CHILDREN = set()       # running child processes (each leads its own process group)


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


def redact(obj):
    """Copy of obj with every configured credential value replaced by a placeholder."""
    if isinstance(obj, str):
        for sec in SECRETS:
            obj = obj.replace(sec, REDACTED)
        return obj
    if isinstance(obj, dict):
        return {redact(k): redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def progress(phase=None, pct=None, message=None, state=None):
    """Report job progress (no-op without --job-file). pct is the percentage of the current phase when measurable."""
    if PROGRESS_HOOK:
        PROGRESS_HOOK(phase, pct, message, state)


def set_result(obj):
    global LAST_RESULT
    LAST_RESULT = obj


def emit_json(obj):
    """The one JSON document of a --json run (to the real stdout; everything else goes to stderr)."""
    set_result(obj)
    print(json.dumps(redact(obj), indent=2, sort_keys=False, default=str), file=JSON_OUT or sys.stdout, flush=True)


def kill_group(proc, grace=5):
    """Stop a child and everything it spawned (it leads its own process group)."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    for fh in (proc.stdout, proc.stdin):
        if fh:
            try:
                fh.close()
            except OSError:
                pass


def run(cmd, log=None, cwd=None, env=None, check=False, timeout=None):
    """Run a command capturing stdout+stderr; mirrors output into the log when given. The child gets no stdin and
    its own process group, so a cancellation (SIGTERM/Ctrl-C) stops it together with everything it spawned."""
    if log:
        log(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""), echo=False)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError as ex:
        raise ClimgError(f"command not found: {cmd[0]} ({ex})")
    CHILDREN.add(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except BaseException:
        kill_group(proc)
        raise
    finally:
        CHILDREN.discard(proc)
    p = subprocess.CompletedProcess(cmd, proc.returncode, out, None)
    if log and p.stdout:
        for ln in p.stdout.rstrip().splitlines():
            log.fh.write("    " + ln + "\n")
            if LOG_HOOK:
                LOG_HOOK("    " + ln)
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
        if LOG_HOOK:
            LOG_HOOK(msg)
        if echo:
            print(msg, flush=True)
