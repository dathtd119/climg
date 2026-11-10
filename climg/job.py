"""Job progress file (`--job-file PATH`): {state, phase, pct, message, log, started, updated, result?}, rewritten
atomically (temp file + rename) so a reader never sees a half-written document."""

import json
import os
import time

from . import util

STATES = ("queued", "running", "succeeded", "failed", "cancelled")
PHASES = ("wait", "download", "extract", "build", "template", "done")
LOG_TAIL = 40


class JobFile:
    MIN_INTERVAL = 0.5   # seconds between rewrites that change neither state nor phase

    def __init__(self, path, phase="download"):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.started = util.now().isoformat(timespec="seconds")
        self.doc = {"state": "running", "phase": phase, "pct": 0, "message": "", "log": [], "started": self.started,
                    "updated": self.started}
        self._last = 0.0
        self._resume = None
        self._write(force=True)

    # progress hook (util.PROGRESS_HOOK)
    def progress(self, phase=None, pct=None, message=None, state=None):
        d = self.doc
        old = (d["state"], d["phase"])
        if phase == "wait" and d["phase"] != "wait":
            self._resume = d["phase"]
        elif phase == "resume":          # a wait ended: back to the phase that was interrupted
            phase = self._resume or "download"
        if phase:
            d["state"] = state or ("queued" if phase == "wait" else "running")
            if phase != d["phase"]:
                d["pct"] = 0
            d["phase"] = phase
        elif state:
            d["state"] = state
        if pct is not None:
            d["pct"] = pct
        if message is not None:
            d["message"] = message
        self._write(force=(d["state"], d["phase"]) != old)

    # log hook (util.LOG_HOOK)
    def log_line(self, line):
        self.doc["log"] = (self.doc["log"] + [str(line)])[-LOG_TAIL:]
        self._write()

    def finish(self, state, result, message=""):
        d = self.doc
        d["state"] = state
        if state == "succeeded":
            d["phase"], d["pct"] = "done", 100
        d["message"] = message or state
        d["result"] = result
        self._write(force=True)

    def _write(self, force=False):
        t = time.monotonic()
        if not force and t - self._last < self.MIN_INTERVAL:
            return
        self._last = t
        self.doc["updated"] = util.now().isoformat(timespec="seconds")
        tmp = f"{self.path}.{os.getpid()}.tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(util.redact(self.doc), f, default=str)
            f.write("\n")
        os.replace(tmp, self.path)
