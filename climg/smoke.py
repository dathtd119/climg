"""`climg test`: one-node lab, wait healthy, SSH login + show version, collect logs, always destroy."""

import json
import os
import re
import subprocess
import time

import yaml

from . import dockerx, registry, template
from .util import Log, now, run

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
    "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "PreferredAuthentications=keyboard-interactive,password", "-o", "PubkeyAuthentication=no",
    "-o", "ConnectTimeout=10",
]


def ssh_probe(kind, creds, ip, run_dir, log):
    """-> (login_state, cmd_state, show_match, output). login_state: ok/ssh_refused/auth_failed/
    forced_pw_change/no_prompt."""
    import pexpect  # lazy: only `test` needs it
    user, pw = creds["username"], creds["password"]
    prompt = re.compile(r"(?:^|[\r\n])" + kind.ssh_prompt + r"[ \t]*$")
    more = re.compile(r"--\s*[Mm]ore\s*--|<--- More --->")
    transcript = open(os.path.join(run_dir, "ssh.txt"), "a")
    child = pexpect.spawn("ssh", SSH_OPTS + [f"{user}@{ip}"], encoding="utf-8", codec_errors="replace", timeout=60)
    child.logfile_read = transcript
    pw_sent = 0
    try:
        while True:
            i = child.expect([r"(?i)are you sure you want to continue", r"(?i)new password",
                              r"(?i)password:\s*$", r"(?i)permission denied", r"(?i)press 'a' to accept", prompt,
                              r"(?i)connection refused|no route to host|connection timed out|connection reset|"
                              r"closed by remote|kex_exchange", pexpect.EOF, pexpect.TIMEOUT])
            if i == 0:
                child.sendline("yes")
            elif i == 1:
                return "forced_pw_change", None, None, child.before
            elif i == 2:
                if pw_sent >= 1:
                    return "auth_failed", None, None, child.before
                child.sendline(pw)
                pw_sent += 1
            elif i == 3:
                return "auth_failed", None, None, child.before
            elif i == 4:
                child.send("a")
            elif i == 5:
                break
            elif i == 6:
                return "ssh_refused", None, None, child.before
            elif i == 7:
                return ("auth_failed" if pw_sent else "ssh_refused"), None, None, child.before
            else:
                return "no_prompt", None, None, child.before
        log(f"  ssh login ok as {user}")
        out = ""
        for c in list(kind.probe.get("pre") or []) + [kind.probe["cmd"]]:
            child.sendline(c)
            out = ""
            while True:
                j = child.expect([more, prompt, pexpect.EOF, pexpect.TIMEOUT], timeout=60)
                out += child.before
                if j == 0:
                    child.send(" ")
                    continue
                break
            if j != 1:
                return "ok", "cmd_no_prompt", None, out
        with open(os.path.join(run_dir, "show.txt"), "w") as f:
            f.write(out)
        m = re.search(kind.probe["regex"], out)
        try:
            child.sendline("exit")
            child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=5)
        except Exception:
            pass
        return "ok", ("ok" if m else "mismatch"), (m.group(0).strip() if m else None), out
    finally:
        child.close(force=True)
        transcript.close()


def console_tail(ip, run_dir, seconds=30):
    """Best effort: read the VM serial console (telnet <ip> 5000)."""
    try:
        import pexpect
        c = pexpect.spawn("telnet", [ip, "5000"], encoding="utf-8", codec_errors="replace", timeout=seconds)
        c.sendline("")
        try:
            c.expect(pexpect.EOF, timeout=seconds)
        except pexpect.TIMEOUT:
            pass
        data = c.before or ""
        c.close(force=True)
    except Exception as ex:
        data = f"console capture failed: {ex!r}"
    with open(os.path.join(run_dir, "console.txt"), "w") as f:
        f.write(data)


def run_test(cfg, kind, tag, key, budget=None):
    """Returns the result record (also appended to results.jsonl). Lab name is always `climg-...`."""
    creds = cfg.need_credentials()
    log = Log(cfg, "test-" + tag.replace("/", "_").replace(":", "_"))
    ts = now()
    stamp = ts.strftime("%Y%m%d-%H%M%S")
    lab = f"climg-{kind.alias}-{stamp}".lower()
    lab = re.sub(r"[^a-z0-9-]", "-", lab)
    run_dir = os.path.join(cfg.runs_dir, f"{stamp}-{lab}")
    os.makedirs(run_dir)
    topo = os.path.join(run_dir, "smoke.clab.yml")
    budget = int(budget or kind.boot_budget_s)
    rec = {"ts": ts.isoformat(timespec="seconds"), "key": key, "kind": kind.name, "image": tag, "image_id": None,
           "stages": {"build": None, "boot": None, "login": None, "cmd": None}, "boot_s": None, "restarts": 0,
           "show_match": None, "result": "FAIL", "fail_stage": None, "fail_class": None, "diagnosis": None,
           "budget_s": budget, "run_dir": run_dir}

    def fail(stage, cls, diag):
        rec["stages"][stage] = "fail"
        rec["fail_stage"], rec["fail_class"], rec["diagnosis"] = stage, cls, str(diag)
        log(f"FAIL at {stage}: {cls} - {diag}")

    img = dockerx.inspect_tag(tag)
    if not img:
        fail("build", "build_failed", f"{tag} not present; run `climg pull {key or tag} --yes` first")
        return finish(cfg, rec, log)
    rec["stages"]["build"] = "ok"
    rec["image_id"] = img["Id"]
    node = {"kind": kind.name, "image": tag, "credentials": {"username": creds["username"],
                                                              "password": creds["password"]}}
    env = template.node_env(kind)
    if env:
        node["env"] = env
    with open(topo, "w") as f:
        yaml.safe_dump({"name": lab, "topology": {"nodes": {"dut": node}}}, f, sort_keys=False)
    cname = f"clab-{lab}-dut"
    log(f"test {tag}: lab {lab}, budget {budget}s, run dir {run_dir}")
    deployed = False
    try:
        deployed = True   # destroy even when deploy half-succeeds
        p = run(["containerlab", "deploy", "-t", topo], log=log, cwd=run_dir)
        if p.returncode != 0:
            fail("boot", "deploy_failed", (p.stdout or "").strip().splitlines()[-1:] or "deploy rc!=0")
            return finish(cfg, rec, log)
        t0 = time.time()
        while True:
            el = int(time.time() - t0)
            st = run(["docker", "inspect", "-f",
                      "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", cname]
                     ).stdout.strip().split()
            state, health = (st + ["", ""])[:2]
            logs_txt = run(["docker", "logs", cname]).stdout or ""
            restarts = len(re.findall(r"(?i)VM failed - restarting|disconnected, restarting|KVM error, restarting",
                                      logs_txt))
            if state != "running":
                fail("boot", "container_exit", f"container state={state} after {el}s")
                break
            if health == "healthy":
                rec["boot_s"] = el
                rec["stages"]["boot"] = "ok"
                log(f"  healthy after {el}s (restarts={restarts})")
                break
            if restarts >= 2:
                fail("boot", "boot_loop", f"{restarts} VM restarts within {el}s")
                break
            if el > budget:
                fail("boot", "boot_timeout", f"health={health} after {el}s (budget {budget}s)")
                break
            if el % 60 < 15:
                log(f"  waiting: state={state} health={health} {el}s")
            time.sleep(15)
        rec["restarts"] = restarts
        pi = subprocess.run(["containerlab", "inspect", "-t", topo, "-f", "json"], text=True, capture_output=True)
        ip = None
        try:
            for nodes in json.loads(pi.stdout or "{}").values():
                for n in nodes:
                    if n.get("name") == cname and n.get("ipv4_address", "N/A") != "N/A":
                        ip = n["ipv4_address"].split("/")[0]
        except ValueError:
            pass
        log(f"  mgmt ip {ip}")
        if rec["stages"]["boot"] == "ok" and ip:
            login_state = None
            for attempt in range(1, 11):
                login_state, cmd_state, match, out = ssh_probe(kind, creds, ip, run_dir, log)
                if login_state == "ok" or (login_state in ("auth_failed", "forced_pw_change") and attempt >= 3):
                    break
                log(f"  ssh attempt {attempt}: {login_state}; retry in 30s")
                time.sleep(30)
            if login_state == "ok":
                rec["stages"]["login"] = "ok"
                if cmd_state == "ok":
                    rec["stages"]["cmd"] = "ok"
                    rec["show_match"] = match
                    rec["result"] = "PASS"
                    log(f"  probe '{kind.probe['cmd']}' matched: {match}")
                else:
                    fail("cmd", "cmd_mismatch", f"{cmd_state}; output head: {out.strip()[:200]!r}")
            else:
                cls = {"ssh_refused": "healthy_no_ssh", "no_prompt": "healthy_no_ssh"}.get(login_state, login_state)
                fail("login", cls, f"{login_state} as {creds['username']} after retries; see ssh.txt")
        if rec["result"] != "PASS" and ip:
            console_tail(ip, run_dir)
        return finish(cfg, rec, log)
    except Exception as ex:
        if not rec["fail_class"]:
            fail(next((k for k, v in rec["stages"].items() if v is None), "boot"), "harness_error", repr(ex))
        return finish(cfg, rec, log)
    finally:
        if deployed:
            dl = run(["docker", "logs", cname]).stdout
            if dl:
                with open(os.path.join(run_dir, "docker.log"), "w") as f:
                    f.write(dl)
        d = run(["containerlab", "destroy", "-t", topo, "--cleanup"], log=log, cwd=run_dir)
        log(f"  destroyed lab {lab} (rc={d.returncode})")


def finish(cfg, rec, log):
    registry.append_result(cfg, rec)
    log(f"result {rec['result']} stages={rec['stages']} fail_class={rec['fail_class']} -> {cfg.results_file}")
    return rec
