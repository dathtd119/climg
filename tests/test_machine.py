"""Machine interface contract (openspec change clab-app-images-explorer, spec climg-machine-interface).

docker, make and the network are fakes: shell scripts on PATH and a patched urlopen. Nothing real is downloaded or built.
"""

import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from climg import CONTRACT_VERSION, build, job as jobmod, util  # noqa: E402
from climg.cli import main, make_parser  # noqa: E402
from climg.config import Config  # noqa: E402
from climg.util import ClimgError  # noqa: E402

SECRET = "s3cr3t-TEST-pw"
LOCAL = "PA-VM-KVM-11.2.5.qcow2"
TAG = "vrnetlab/paloalto_pa-vm:11.2.5"

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/docker.log"
IMG='[{"Id":"sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","Size":1000,"RepoTags":["vrnetlab/paloalto_pa-vm:11.2.5"],"Config":{"Labels":{"org.opencontainers.image.authors":"roman@dodin.dev"},"Entrypoint":["uv","run","/launch.py"]}}]'
case "$1 $2" in
  "image inspect") if [ -f "$FAKE_DIR/built" ]; then echo "$IMG"; exit 0; fi; exit 1;;
  "image ls") if [ -f "$FAKE_DIR/built" ]; then echo sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef; fi; exit 0;;
esac
exit 0
"""

FAKE_MAKE = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/make.log"
case "$*" in *version-test*) echo "Version: 11.2.5"; exit 0;; esac
echo cid123 > cidfile
[ -n "$FAKE_FAIL" ] && { echo "boom from make"; exit 2; }
if [ -n "$FAKE_SLEEP" ]; then touch "$FAKE_DIR/building"; sleep 60; fi
touch "$FAKE_DIR/built"
"""


def fake_index(entries):
    return {"schema_version": "1.0", "url_properties": {
        "protocol": "https", "hostnames": {"main": "m.example"}, "prefixes": {"main": "/0:"}},
        "QEMU": entries, "IOL": [], "DYNAMIPS": []}


class Env(unittest.TestCase):
    """A throw-away climg installation: config, state dir, vrnetlab checkout, vendor image, fake docker/make."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.state, self.fake, self.bin = (os.path.join(t, d) for d in ("state", "fake", "bin"))
        self.images, self.vr = os.path.join(t, "images"), os.path.join(t, "vr")
        self.tplfile = os.path.join(t, "ws", "ui", "custom-nodes.json")
        for d in (self.state, self.fake, self.bin, self.images, os.path.join(self.vr, "common"),
                  os.path.join(self.vr, "paloalto", "pan")):
            os.makedirs(d)
        open(os.path.join(self.vr, "makefile.include"), "w").close()
        open(os.path.join(self.vr, "paloalto", "pan", "Makefile"), "w").close()
        with open(os.path.join(self.images, LOCAL), "wb") as f:
            f.write(b"disk")
        for name, body in (("docker", FAKE_DOCKER), ("make", FAKE_MAKE)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.cfgfile = os.path.join(t, "config.yaml")
        self.write_config()
        self.env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "FAKE_DIR": self.fake}

    def write_config(self, **extra):
        import yaml
        cfg = {"state_dir": self.state, "vrnetlab": self.vr, "images_dirs": [self.images], "disk_floor_gb": 0,
               "max_builds": 1, "workspace": os.path.join(self.tmp, "ws"), "nodetpl_file": self.tplfile,
               "nodetpl_writer": "file", "credentials": {"username": "admin", "password": SECRET}}
        cfg.update(extra)
        with open(self.cfgfile, "w") as f:
            yaml.safe_dump(cfg, f)

    def cli(self, *argv, config=True, env=None, index=True):
        """Run climg.cli.main in-process; returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        idx = mock.patch("climg.index.load_index", return_value=(fake_index([]), {})) if index else \
            contextlib.nullcontext()
        full = ["--config", self.cfgfile] + list(argv) if config else list(argv)
        with mock.patch.dict(os.environ, {**self.env, **(env or {})}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), idx:
            try:
                rc = main(full)
            except SystemExit as ex:
                rc = ex.code
        return rc, out.getvalue(), err.getvalue()

    def one_json(self, out):
        """stdout must be exactly one JSON document."""
        return json.loads(out)


class JsonOutput(Env):
    def test_clean_stdout_with_warning_on_stderr(self):       # Requirement: JSON output / Clean stdout
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("climg.index.load_index", side_effect=ClimgError("index down")):
            rc = main(["--config", self.cfgfile, "search", "--json", "--all", "--", "pa"])
        self.assertEqual(rc, 0)
        self.assertIsInstance(json.loads(out.getvalue()), list)
        self.assertIn("warning: LabHub index unavailable", err.getvalue())
        self.assertNotIn("warning", out.getvalue())

    def test_nodetpl_sync_dry_run_lists_changes(self):        # Mutating command result
        images = [{"id": "sha256:" + "0" * 64, "tags": [TAG], "size": 1, "labels": {}, "entrypoint": [],
                   "vrnetlab": True, "dangling": False, "created": ""}]
        with mock.patch("climg.dockerx.list_images", return_value=images):
            rc, out, err = self.cli("nodetpl", "--json", "--writer", "file", "--dry-run", "--", "sync")
        self.assertEqual(rc, 0, err)
        doc = self.one_json(out)
        self.assertTrue(doc["dry_run"])
        self.assertEqual(doc["added"], ["paloalto-pa-vm-11.2.5"])
        self.assertEqual(doc["removed"], [])
        self.assertFalse(os.path.exists(self.tplfile))

    def test_nodetpl_kept_in_json(self):
        images = [{"id": "sha256:" + "0" * 64, "tags": [TAG], "size": 1, "labels": {}, "entrypoint": [],
                   "vrnetlab": True, "dangling": False, "created": ""}]
        with mock.patch("climg.dockerx.list_images", return_value=images):
            rc, out, err = self.cli("nodetpl", "--json", "--writer", "file", "--", "sync")
            self.assertEqual(rc, 0, err)
            with open(self.tplfile) as f:
                data = json.load(f)
            for c in data:
                if c["name"] == "paloalto-pa-vm-11.2.5":
                    c["interfacePattern"] = "ge-{n}"
                    c["icon"] = "switch"
                    c["baseName"] = "old"
            with open(self.tplfile, "w") as f:
                json.dump(data, f)
            owned = os.path.join(self.state, "nodetpl-owned.json")
            with open(owned) as f:
                doc = json.load(f)
            doc["generated"]["paloalto-pa-vm-11.2.5"].update(icon="router", baseName="old")   # as if climg's output changed
            with open(owned, "w") as f:
                json.dump(doc, f)
            rc, out, err = self.cli("nodetpl", "--json", "--writer", "file", "--dry-run", "--", "sync")
        doc = self.one_json(out)
        self.assertEqual(doc["changes"], [{"action": "updated", "name": "paloalto-pa-vm-11.2.5",
                                           "kept": ["icon", "interfacePattern"]}])
        self.assertEqual(doc["updated"], ["paloalto-pa-vm-11.2.5"])

    def test_json_on_each_command(self):                      # tasks 2.1: pull/add/build/template/nodetpl add|remove
        self.assertEqual(self.build_local("--no-nodetpl")["status"], "built")                       # build
        rc, out, _ = self.cli("add", "--json", "--", os.path.join(self.images, LOCAL))
        self.assertEqual(rc, 0)
        self.assertEqual(self.one_json(out)["status"], "registered")                  # add
        rc, out, _ = self.cli("pull", "--yes", "--json", "--", "local:" + LOCAL)
        self.assertEqual((rc, self.one_json(out)["status"]), (0, "noop"))             # pull (already built)
        rc, out, _ = self.cli("template", "--json", "--", "local:" + LOCAL)
        self.assertEqual(rc, 0)
        self.assertEqual(self.one_json(out)["tag"], TAG)                              # template
        rc, out, err = self.cli("nodetpl", "--json", "--writer", "file", "--", "add", "local:" + LOCAL)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.one_json(out)["added"], ["paloalto-pa-vm-11.2.5"])      # nodetpl add
        rc, out, _ = self.cli("nodetpl", "--json", "--writer", "file", "--", "remove", "paloalto-pa-vm-11.2.5")
        self.assertEqual(self.one_json(out)["removed"], ["paloalto-pa-vm-11.2.5"])    # nodetpl remove

    def build_local(self, *extra, env=None):
        rc, out, err = self.cli("build", "--yes", "--json", *extra, "--", "local:" + LOCAL, env=env)
        self.assertEqual(rc, 0, err)
        return self.one_json(out)


class ErrorEnvelopeAndExitCodes(Env):
    def test_unknown_reference_not_found(self):               # JSON error envelope / Unknown reference
        rc, out, _ = self.cli("show", "--json", "--", "nope")
        self.assertEqual(rc, 1)
        doc = self.one_json(out)
        self.assertEqual(doc["error"]["code"], "not_found")
        self.assertIsInstance(doc["error"]["message"], str)

    def test_invalid_ref(self):
        rc, out, _ = self.cli("show", "--json", "--", "#abc")
        self.assertEqual((rc, self.one_json(out)["error"]["code"]), (1, "invalid_ref"))

    def test_no_config(self):
        rc, out, _ = self.cli("--config", os.path.join(self.tmp, "missing.yaml"), "images", "--json", config=False)
        self.assertEqual((rc, self.one_json(out)["error"]["code"]), (1, "no_config"))

    def test_disk_floor(self):
        cfg = Config({"state_dir": self.state, "disk_floor_gb": 10 ** 9}, "x")
        with self.assertRaises(ClimgError) as c:
            build.disk_check(cfg, 1, lambda *a, **k: None, "t")
        self.assertEqual(c.exception.code, "disk_floor")

    def test_build_failed(self):
        rc, out, _ = self.cli("build", "--yes", "--json", "--no-nodetpl", "--", "local:" + LOCAL,
                              env={"FAKE_FAIL": "1"})
        self.assertEqual(rc, 1)
        self.assertEqual(self.one_json(out)["error"]["code"], "build_failed")
        self.assertEqual(os.listdir(os.path.join(self.state, "work")), [])

    def test_download_failed(self):
        cfg = Config({"state_dir": self.state, "mirrors": []}, "x")
        f = {"checksum": {"md5": "a" * 32}, "size": 1, "filename": "x", "path": "/x"}
        with self.assertRaises(ClimgError) as c:
            build.download(cfg, fake_index([]), f, lambda *a, **k: None)
        self.assertEqual(c.exception.code, "download_failed")

    def test_needs_yes_exit_3(self):                          # Distinct exit codes / Missing confirmation
        rc, out, _ = self.cli("pull", "--json", "--", "local:" + LOCAL)
        self.assertEqual(rc, 3)
        self.assertEqual(self.one_json(out)["error"]["code"], "needs_yes")
        rc, out, _ = self.cli("pull", "--", "local:" + LOCAL)   # text mode: same code, same message as before
        self.assertEqual(rc, 3)
        self.assertIn("Re-run with --yes to proceed (nothing was changed).", out)

    def test_exit_codes_0_1_2_130(self):
        self.assertEqual(self.cli("scan", "--json")[0], 0)
        self.assertEqual(self.cli("show", "--json", "--", "nope")[0], 1)
        self.assertEqual(self.cli("pull", "--bogus-flag")[0], 2)                    # usage error: argparse only
        self.assertEqual(self.cli("nodetpl", "list", "--json", "--", "x")[0], 2)     # usage error
        with mock.patch("climg.cli.cmd_scan", side_effect=KeyboardInterrupt):
            self.assertEqual(self.cli("scan")[0], 130)

    def test_nodetpl_needs_target_is_runtime_error_not_usage(self):
        rc, out, _ = self.cli("nodetpl", "--json", "--writer", "file", "--", "add")
        self.assertEqual((rc, self.one_json(out)["error"]["code"]), (1, "invalid_ref"))


class JobFileTests(Env):
    def read(self, p):
        with open(p) as f:
            return json.load(f)

    def test_download_then_build_then_succeeded(self):         # Job progress file / Download progress
        payload = b"x" * (4 << 20)
        entry = {"id": 7, "name": "paloalto-11.2.5", "type": "qemu", "metadata": {}, "files": [{
            "filename": LOCAL, "path": "/addons/qemu/pa/" + LOCAL, "size": len(payload), "file_type": "x",
            "extension": ".qcow2", "checksum": {"md5": hashlib.md5(payload).hexdigest(),
                                                "sha1": hashlib.sha1(payload).hexdigest()}}]}

        class Resp:
            headers, status = {"Content-Type": "application/octet-stream"}, 200

            def __init__(self):
                self.data = io.BytesIO(payload)

            def read(self, n=-1):
                return self.data.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        hist = []
        orig = jobmod.JobFile.progress

        def rec(job, *a, **k):
            orig(job, *a, **k)
            hist.append((job.doc["state"], job.doc["phase"], job.doc["pct"]))

        jf = os.path.join(self.tmp, "jobs", "j1.json")
        with mock.patch("climg.index.load_index", return_value=(fake_index([entry]), {})), \
                mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: Resp()), \
                mock.patch.object(jobmod.JobFile, "progress", rec):
            rc, out, err = self.cli("pull", "--yes", "--json", "--job-file", jf, "--", "labhub:paloalto-11.2.5",
                                    index=False)
        self.assertEqual(rc, 0, err)
        down = [pct for st, ph, pct in hist if ph == "download"]
        self.assertTrue(down and down == sorted(down) and down[-1] == 100, hist)
        phases = [ph for _, ph, _ in hist]
        self.assertLess(phases.index("download"), phases.index("build"))
        self.assertLess(phases.index("build"), phases.index("template"))
        final = self.read(jf)
        self.assertEqual((final["state"], final["phase"]), ("succeeded", "done"))
        self.assertEqual(final["result"], self.one_json(out))     # same object as --json
        for k in ("state", "phase", "pct", "message", "log", "started", "updated"):
            self.assertIn(k, final)
        self.assertIn(final["state"], jobmod.STATES)
        self.assertTrue({ph for _, ph, _ in hist} <= set(jobmod.PHASES))
        self.assertTrue(os.path.exists(self.tplfile))              # template written (no --no-nodetpl)

    def test_failed_job_file_holds_error_result(self):
        jf = os.path.join(self.tmp, "j.json")
        rc, out, _ = self.cli("build", "--yes", "--json", "--no-nodetpl", "--job-file", jf, "--",
                              "local:" + LOCAL, env={"FAKE_FAIL": "1"})
        final = self.read(jf)
        self.assertEqual((rc, final["state"]), (1, "failed"))
        self.assertEqual(final["result"], self.one_json(out))
        self.assertEqual(final["result"]["error"]["code"], "build_failed")

    def test_waiting_for_build_slot_is_queued_wait(self):      # Waiting for a build slot
        cfg = Config({"state_dir": self.state, "max_builds": 1}, "x")
        os.makedirs(cfg.locks_dir)
        holder = open(os.path.join(cfg.locks_dir, "build-slot-0.lock"), "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        jf = os.path.join(self.tmp, "wait.json")
        job = jobmod.JobFile(jf, "download")
        util.PROGRESS_HOOK = job.progress
        self.addCleanup(setattr, util, "PROGRESS_HOOK", None)
        got = {}
        with mock.patch.object(build, "SLOT_POLL", 0.05):
            th = threading.Thread(target=lambda: got.setdefault("slot", build.acquire_build_slot(
                cfg, lambda *a, **k: None)))
            th.start()
            deadline = time.time() + 5
            while time.time() < deadline and self.read(jf)["state"] != "queued":
                time.sleep(0.02)
            waiting = self.read(jf)
            self.assertEqual((waiting["state"], waiting["phase"]), ("queued", "wait"))
            holder.close()                                      # a slot frees
            th.join(5)
        self.assertIn("slot", got)
        after = self.read(jf)
        self.assertEqual(after["state"], "running")
        self.assertNotEqual(after["phase"], "wait")
        got["slot"].close()

    def test_atomic_write_leaves_no_temp_file(self):
        jf = os.path.join(self.tmp, "a", "j.json")
        job = jobmod.JobFile(jf)
        for i in range(5):
            job.progress("download", i * 10, "m")
            json.load(open(jf))
        job.finish("succeeded", {"ok": True})
        self.assertEqual(os.listdir(os.path.dirname(jf)), ["j.json"])

    def test_nodetpl_sync_job_file(self):
        jf = os.path.join(self.tmp, "sync.json")
        with mock.patch("climg.dockerx.list_images", return_value=[]):
            rc, out, _ = self.cli("nodetpl", "--json", "--writer", "file", "--dry-run", "--job-file", jf, "--", "sync")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(jf)["state"], "succeeded")


class Cancellation(Env):
    def start(self, *extra, fast=False):
        env = {**os.environ, **self.env}
        if not fast:
            env["FAKE_SLEEP"] = "1"
        self.jf = os.path.join(self.tmp, "cancel.json")
        return subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "climg"), "--config", self.cfgfile,
                                 "build", "--yes", "--json", "--job-file", self.jf, "--no-nodetpl", *extra, "--",
                                 "local:" + LOCAL], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def test_sigterm_during_build(self):                       # Cancellation / Cancel during build
        p = self.start()
        building = os.path.join(self.fake, "building")
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(building) and p.poll() is None:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(building), "fake make never started")
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 4, err)
        self.assertEqual(json.loads(out)["error"]["code"], "cancelled")
        self.assertEqual(json.load(open(self.jf))["state"], "cancelled")
        self.assertEqual(os.listdir(os.path.join(self.state, "work")), [])        # work copy gone
        with open(os.path.join(self.fake, "docker.log")) as f:
            self.assertIn("rm -f cid123", f.read())                                # cidfile container removed
        for fn in os.listdir(os.path.join(self.state, "locks")):                   # no lock is held
            with open(os.path.join(self.state, "locks", fn)) as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ps = subprocess.run(["pgrep", "-f", os.path.join(self.fake, "building")], capture_output=True)
        self.assertNotEqual(ps.returncode, 0)
        # a following build of the same image succeeds
        p2 = self.start(fast=True)
        out2, err2 = p2.communicate(timeout=60)
        self.assertEqual(p2.returncode, 0, err2)
        self.assertEqual(json.loads(out2)["status"], "built")
        self.assertEqual(json.load(open(self.jf))["state"], "succeeded")

    def test_partial_download_survives_cancel(self):
        cfg = Config({"state_dir": self.state}, "x")
        part = os.path.join(cfg.downloads_dir, "a" * 32, "f.part")
        os.makedirs(os.path.dirname(part))
        open(part, "w").write("partial")
        with self.assertRaises(util.Cancelled):
            with mock.patch("urllib.request.urlopen", side_effect=util.Cancelled):
                build.download(cfg, fake_index([]), {"checksum": {"md5": "a" * 32, "sha1": ""}, "size": 100,
                                                     "filename": "f", "path": "/f"}, lambda *a, **k: None)
        self.assertTrue(os.path.exists(part))


class TemplateStepControl(Env):
    def test_no_nodetpl_leaves_file_unchanged(self):           # Build without template write
        doc = self.build_local_json("--no-nodetpl")
        self.assertFalse(os.path.exists(self.tplfile))
        self.assertEqual(doc["nodetpl"]["name"], "paloalto-pa-vm-11.2.5")
        self.assertFalse(doc["nodetpl"]["written"])

    def test_without_flag_template_is_written(self):
        doc = self.build_local_json()
        self.assertTrue(doc["nodetpl"]["written"])
        self.assertTrue(os.path.exists(self.tplfile))

    def build_local_json(self, *extra):
        rc, out, err = self.cli("build", "--yes", "--json", *extra, "--", "local:" + LOCAL)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_pull_and_add_accept_no_nodetpl(self):
        p = make_parser()
        for argv in (["pull", "--no-nodetpl", "--", "x"], ["add", "--build", "--no-nodetpl", "--", "f"],
                     ["build", "--no-nodetpl", "--", "x"]):
            self.assertTrue(p.parse_args(argv).no_nodetpl)

    def test_nodetpl_file_flag_and_env(self):
        other = os.path.join(self.tmp, "other.json")
        with open(other, "w") as f:
            json.dump([{"name": "mine", "kind": "linux", "image": "x"}], f)
        rc, out, err = self.cli("nodetpl", "--json", "--writer", "file", "--nodetpl-file", other, "--", "list")
        self.assertEqual((rc, [t["name"] for t in self.one_json(out)]), (0, ["mine"]), err)
        rc, out, err = self.cli("nodetpl", "--json", "--writer", "file", "--", "list",
                                env={"CLIMG_NODETPL_FILE": other})
        self.assertEqual([t["name"] for t in self.one_json(out)], ["mine"])


class ConfigAndVersion(Env):
    def test_config_json_has_no_secret(self):                  # Effective configuration
        rc, out, _ = self.cli("config", "--json")
        self.assertEqual(rc, 0)
        self.assertNotIn(SECRET, out)
        doc = self.one_json(out)
        self.assertEqual(doc["images_dirs"], [self.images])
        self.assertEqual(doc["disk_floor_gb"], 0)
        for k in ("state_dir", "max_builds", "mirrors", "contract_version"):
            self.assertIn(k, doc)
        self.assertNotIn("credentials", doc)
        self.assertEqual(doc["contract_version"], CONTRACT_VERSION)

    def test_version_prints_contract(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as c:
            main(["--version"])
        self.assertEqual(c.exception.code, 0)
        self.assertIn(f"contract {CONTRACT_VERSION}", out.getvalue())


class NonInteractive(Env):
    def test_init_json_without_yes_exits_3(self):              # Init from a program
        new_cfg = os.path.join(self.tmp, "new", "config.yaml")
        with mock.patch("builtins.input", side_effect=AssertionError("stdin was read")):
            rc, out, _ = self.cli("--config", new_cfg, "init", "--json", config=False)
        self.assertEqual(rc, 3)
        self.assertEqual(self.one_json(out)["error"]["code"], "needs_yes")
        self.assertFalse(os.path.exists(new_cfg))

    def test_init_without_tty_and_yes_exits_3_text(self):
        new_cfg = os.path.join(self.tmp, "new", "config.yaml")
        with mock.patch("sys.stdin.isatty", return_value=False):
            rc, out, _ = self.cli("--config", new_cfg, "init", config=False)
        self.assertEqual(rc, 3)
        self.assertIn("re-run with --yes", out)

    def test_stdin_replaced_under_yes_and_json(self):
        seen = {}

        def probe(args, cfg, kinds):
            seen["data"] = sys.stdin.read()
            return 0

        with mock.patch("climg.cli.cmd_scan", probe), mock.patch("sys.stdin", io.StringIO("typed")):
            self.cli("scan", "--json")
        self.assertEqual(seen["data"], "")


class SecretFree(Env):
    def test_template_json_redacts_password(self):            # Template command
        rc, out, err = self.cli("template", "--json", "--", "local:" + LOCAL)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(SECRET, out + err)
        self.assertEqual(self.one_json(out)["topology"]["topology"]["kinds"]["paloalto_panos"]["credentials"],
                         {"username": "admin", "password": util.REDACTED})

    def test_template_text_keeps_real_password_in_file(self):  # text output unchanged: the file is a real lab file
        d = os.path.join(self.tmp, "tpl")
        rc, out, err = self.cli("template", "-o", d, "--", "local:" + LOCAL)
        self.assertEqual(rc, 0, err)
        self.assertIn("wrote ", out)
        self.assertIn(SECRET, open(os.path.join(d, os.listdir(d)[0])).read())

    def test_show_json_redacts_password(self):                # Show command
        rc, out, err = self.cli("show", "--json", "--", "local:" + LOCAL)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(SECRET, out + err)
        snip = self.one_json(out)["template_kinds_snippet"]
        self.assertEqual(snip["paloalto_panos"]["credentials"]["password"], util.REDACTED)

    def test_job_file_and_errors_are_redacted(self):
        jf = os.path.join(self.tmp, "r.json")
        util.SECRETS = [SECRET]
        self.addCleanup(setattr, util, "SECRETS", [])
        job = jobmod.JobFile(jf)
        job.log_line(f"login with {SECRET}")
        job.progress("build", 5, f"msg {SECRET}")
        job.finish("failed", {"error": {"code": "x", "message": SECRET}})
        self.assertNotIn(SECRET, open(jf).read())
        self.assertIn(util.REDACTED, open(jf).read())
        self.assertEqual(util.redact({"a": [SECRET + "x"]}), {"a": [util.REDACTED + "x"]})

    def test_error_envelope_redacted(self):
        with mock.patch("climg.cli.cmd_scan", side_effect=ClimgError(f"bad {SECRET}")):
            rc, out, err = self.cli("scan", "--json")
        self.assertEqual(rc, 1)
        self.assertNotIn(SECRET, out + err.replace("error: bad " + SECRET, ""))
        self.assertNotIn(SECRET, out)


class ArgumentForms(unittest.TestCase):
    """The exact command lines the web server builds (options first, then `--`, then all positionals)."""

    def test_server_forms_parse(self):
        p = make_parser()
        a = p.parse_args(["nodetpl", "--json", "--writer", "file", "--nodetpl-file", "/p/custom-nodes.json", "--",
                          "add", "labhub:x"])
        self.assertEqual((a.cmd, a.action, a.target, a.json, a.writer, a.nodetpl_file),
                         ("nodetpl", "add", "labhub:x", True, "file", "/p/custom-nodes.json"))
        a = p.parse_args(["show", "--json", "--", "labhub:x"])
        self.assertEqual((a.cmd, a.ref, a.json), ("show", "labhub:x", True))
        a = p.parse_args(["pull", "--yes", "--json", "--job-file", "/j/1.json", "--no-nodetpl", "--", "labhub:x"])
        self.assertEqual((a.cmd, a.ref, a.yes, a.json, a.job_file, a.no_nodetpl),
                         ("pull", "labhub:x", True, True, "/j/1.json", True))
        a = p.parse_args(["build", "--yes", "--json", "--", "local:my.qcow2"])
        self.assertEqual((a.cmd, a.ref, a.yes, a.json), ("build", "local:my.qcow2", True, True))
        a = p.parse_args(["nodetpl", "--json", "--writer", "file", "--nodetpl-file", "P", "--", "remove", "tpl-name"])
        self.assertEqual((a.action, a.target), ("remove", "tpl-name"))
        a = p.parse_args(["nodetpl", "--json", "--", "sync"])
        self.assertEqual((a.action, a.target), ("sync", None))

    def test_dash_ref_cannot_become_an_option(self):
        a = make_parser().parse_args(["show", "--json", "--", "-x"])
        self.assertEqual(a.ref, "-x")

    def test_every_web_command_accepts_json(self):
        p = make_parser()
        for cmd in (["index"], ["search"], ["show", "r"], ["images"], ["scan"], ["lab"], ["pull", "r"], ["add", "f"],
                    ["build", "r"], ["template", "r"], ["nodetpl", "list"], ["nodetpl", "sync"],
                    ["nodetpl", "add", "r"], ["nodetpl", "remove", "r"], ["doctor"], ["config"], ["init"]):
            self.assertTrue(p.parse_args(cmd + ["--json"]).json, cmd)

    def test_job_file_on_long_commands(self):
        p = make_parser()
        for cmd in (["pull", "r"], ["add", "f"], ["build", "r"], ["nodetpl", "sync"]):
            self.assertEqual(p.parse_args(cmd + ["--job-file", "J"]).job_file, "J", cmd)


if __name__ == "__main__":
    unittest.main()
