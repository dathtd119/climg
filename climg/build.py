"""Fetch (resume, verify), extract, build through a vrnetlab work copy, record. Never edits the vrnetlab checkout."""

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile

from . import dockerx, index as ix, registry
from .catalog import PATCHES_DIR
from .util import UA, ClimgError, free_bytes, gb, run

MAX_STALLS = 8
HTML_RE = re.compile(rb"<!doctype html|<html|<head>|<title>", re.I)


# ---------------------------------------------------------------- locks / disk

def acquire_lock(cfg, name, log, shared_wait=True):
    os.makedirs(cfg.locks_dir, exist_ok=True)
    fh = open(os.path.join(cfg.locks_dir, re.sub(r"[^\w.@-]+", "_", name) + ".lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"another climg run holds {name}; waiting ...")
        fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def acquire_build_slot(cfg, log):
    """Global cap on concurrent builds: one of max_builds flock slots, released when the handle closes."""
    os.makedirs(cfg.locks_dir, exist_ok=True)
    announced = False
    while True:
        for i in range(cfg.max_builds):
            fh = open(os.path.join(cfg.locks_dir, f"build-slot-{i}.lock"), "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                log(f"build slot {i + 1}/{cfg.max_builds} acquired")
                return fh
            except BlockingIOError:
                fh.close()
        if not announced:
            log(f"all {cfg.max_builds} build slots busy; waiting ...")
            announced = True
        time.sleep(10)


def disk_check(cfg, need_extra, log, label):
    """Free space must stay above the floor after `need_extra` more bytes are written."""
    os.makedirs(cfg.state, exist_ok=True)
    free = free_bytes(cfg.state)
    log(f"disk {label}: free {gb(free)}, need {gb(need_extra)} + floor {gb(cfg.disk_floor)}")
    if free - need_extra < cfg.disk_floor:
        raise ClimgError(f"disk floor: {gb(free)} free, {gb(need_extra)} needed, floor {gb(cfg.disk_floor)} "
                         f"({label})")


# ---------------------------------------------------------------- download

def _looks_html(path):
    try:
        with open(path, "rb") as f:
            return bool(HTML_RE.search(f.read(2048)))
    except OSError:
        return False


def _hash_verify(path, f, log):
    log("verifying md5/sha1 ...")
    m, s = hashlib.md5(), hashlib.sha1()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 22), b""):
            m.update(blk)
            s.update(blk)
    ck = f["checksum"]
    return m.hexdigest() == ck["md5"] and (not ck.get("sha1") or s.hexdigest() == ck["sha1"]), m.hexdigest()


def download(cfg, idx, f, log):
    """Download one LabHub file into <downloads>/<md5>/<filename> with Range resume and mirror failover."""
    md5, size = f["checksum"]["md5"], f["size"]
    d = os.path.join(cfg.downloads_dir, md5)
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f["filename"])
    if os.path.exists(dest):
        os.utime(dest)
        return dest
    part = dest + ".part"
    urls = ix.mirror_urls(idx, f["path"], cfg.mirrors)
    if not urls:
        raise ClimgError(f"no usable mirror in config mirrors={cfg.mirrors}")
    md5_fails = 0
    for url in urls:
        stalls, attempt = 0, 0
        while stalls < MAX_STALLS:
            attempt += 1
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have > size:
                os.remove(part)
                have = 0
            if have < size:
                hdr = {"User-Agent": UA}
                if have:
                    hdr["Range"] = f"bytes={have}-"
                log(f"download attempt {attempt}: {url} from byte {have}/{size}")
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as r:
                        if "text/html" in (r.headers.get("Content-Type") or "").lower():
                            raise ClimgError(f"mirror answered with an HTML page (quota/login page?): {url}")
                        if have and r.status != 206:
                            log(f"server ignored Range (HTTP {r.status}); restarting from 0")
                            have = 0
                        got, last, t0 = have, -1, time.time()
                        with open(part, "ab" if have else "wb") as out:
                            while True:
                                buf = r.read(1 << 20)
                                if not buf:
                                    break
                                out.write(buf)
                                got += len(buf)
                                pct = int(got * 100 / size)
                                if pct // 10 != last // 10:
                                    last = pct
                                    log(f"  {pct:3d}%  {gb(got)}  "
                                        f"{(got - have) / max(time.time() - t0, 0.001) / 1e6:.1f} MB/s")
                except ClimgError:
                    raise
                except (urllib.error.URLError, OSError, ConnectionError) as ex:
                    log(f"download error: {ex!r}")
                now_have = os.path.getsize(part) if os.path.exists(part) else 0
                stalls = 0 if now_have > have else stalls + 1
                if now_have < size:
                    log(f"incomplete {now_have}/{size} (stalls {stalls}/{MAX_STALLS}); resuming")
                    time.sleep(min(60, 5 * (stalls + 1)))
                    continue
            if _looks_html(part):
                os.remove(part)
                raise ClimgError("downloaded file is an HTML page (quota/login/error page), not an image")
            ok, got_md5 = _hash_verify(part, f, log)
            if not ok:
                log(f"checksum mismatch (md5 {got_md5} != {md5}); discarding")
                os.remove(part)
                md5_fails += 1
                if md5_fails >= 2:
                    raise ClimgError("checksum mismatch twice; the mirror's file differs from the index")
                continue
            os.rename(part, dest)
            log(f"checksums ok, cached at {dest}")
            return dest
        log(f"mirror gave up after {MAX_STALLS} stalled attempts: {url}")
    raise ClimgError(f"download failed on every mirror ({', '.join(cfg.mirrors)})")


# ---------------------------------------------------------------- extract

def extract(src, kind, dest_path, log, mode):
    """Place the disk image at dest_path: hard link/copy a plain file, stream one member out of an archive."""
    if mode == "plain":
        try:
            os.link(src, dest_path)
            log(f"hard-linked source as {dest_path}")
        except OSError:
            shutil.copyfile(src, dest_path)
            log(f"copied source as {dest_path}")
        return
    low = src.lower()
    wanted = tuple(kind.image_ext)
    if low.endswith(".zip"):
        with zipfile.ZipFile(src) as zf:
            for zi in zf.infolist():
                if not zi.is_dir() and zi.filename.lower().endswith(wanted):
                    log(f"extracting {zi.filename} from {os.path.basename(src)}")
                    with zf.open(zi) as fs, open(dest_path, "wb") as out:
                        shutil.copyfileobj(fs, out, 1 << 22)
                    log(f"extracted {gb(os.path.getsize(dest_path))} -> {dest_path}")
                    return
    else:
        with tarfile.open(src, "r|*") as tf:
            for ti in tf:
                if ti.isfile() and ti.name.lower().endswith(wanted):
                    log(f"extracting {ti.name} from {os.path.basename(src)}")
                    with open(dest_path, "wb") as out:
                        shutil.copyfileobj(tf.extractfile(ti), out, 1 << 22)
                    if os.path.getsize(dest_path) != ti.size:
                        raise ClimgError(f"extracted size {os.path.getsize(dest_path)} != {ti.size}")
                    log(f"extracted {gb(ti.size)} -> {dest_path}")
                    return
    raise ClimgError(f"no {'/'.join(kind.image_ext)} member found in {src}")


# ---------------------------------------------------------------- vrnetlab work copy

def _need_vrnetlab(cfg):
    if not cfg.vrnetlab or not os.path.isdir(cfg.vrnetlab):
        raise ClimgError(f"vrnetlab checkout not found (config 'vrnetlab': {cfg.vrnetlab!r})")
    return cfg.vrnetlab


def prepare_work_copy(cfg, kind, name, log=None, light=False):
    """<work>/<name>/{makefile*.include, common/, <vendor common>, <platform dir>} - the checkout stays untouched."""
    vr = _need_vrnetlab(cfg)
    bdir = os.path.join(cfg.work_dir, name)
    if os.path.exists(bdir):
        shutil.rmtree(bdir)
    os.makedirs(bdir)
    ign = shutil.ignore_patterns("*.qcow2", "*.tgz", "*.vmdk", "*.iso", "__pycache__")
    for fn in os.listdir(vr):
        if fn.startswith("makefile") and fn.endswith(".include"):
            shutil.copy2(os.path.join(vr, fn), bdir)
    if light:
        os.makedirs(os.path.join(bdir, kind.dir))
        shutil.copy2(os.path.join(vr, kind.dir, "Makefile"), os.path.join(bdir, kind.dir))
        return bdir, os.path.join(bdir, kind.dir)
    shutil.copytree(os.path.join(vr, "common"), os.path.join(bdir, "common"), ignore=ign)
    if os.path.isdir(os.path.join(vr, kind.vendor_common)):
        shutil.copytree(os.path.join(vr, kind.vendor_common), os.path.join(bdir, kind.vendor_common), ignore=ign)
    shutil.copytree(os.path.join(vr, kind.dir), os.path.join(bdir, kind.dir), ignore=ign)
    if log:
        log(f"work copy prepared: {bdir}")
    return bdir, os.path.join(bdir, kind.dir)


def apply_patches(kind, bdir, log, patches_dir=PATCHES_DIR):
    """Apply the kind's patches (kinds.yaml `patches`, files in patches/) to the work copy with `patch -p1`.
    Returns [{name, sha256}]; any missing or non-applying patch fails the build."""
    applied = []
    for name in kind.patches:
        path = os.path.join(patches_dir, name)
        if not os.path.isfile(path):
            raise ClimgError(f"patch {name} for {kind.name} not found in {patches_dir}")
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        p = run(["patch", "-p1", "-F", "0", "--batch", "--no-backup-if-mismatch", "-i", path], log=log, cwd=bdir)
        if p.returncode != 0:
            tail = "\n".join((p.stdout or "").splitlines()[-15:])
            raise ClimgError(f"patch {name} (sha256 {digest[:12]}) does not apply to the vrnetlab checkout for "
                             f"{kind.dir} (rc={p.returncode}); the upstream files changed? tail:\n{tail}")
        log(f"patch applied: {name} sha256 {digest}")
        applied.append({"name": name, "sha256": digest})
    return applied


def patch_label(applied):
    """Value of the `climg.patches` image label: name@sha256 entries joined by commas."""
    return ",".join(f"{a['name']}@{a['sha256']}" for a in applied)


def image_patches(im):
    """[{name, sha256}] parsed from the `climg.patches` label of an image dict (dockerx.list_images shape or
    `docker image inspect` output); [] when unpatched."""
    labels = (im or {}).get("labels") or ((im or {}).get("Config") or {}).get("Labels") or {}
    out = []
    for item in filter(None, (labels.get("climg.patches") or "").split(",")):
        name, _, digest = item.partition("@")
        out.append({"name": name, "sha256": digest})
    return out


def _make_env(cfg, kind):
    env = dict(os.environ, GIT_DIR=os.path.join(cfg.vrnetlab, ".git"))
    env.update({k: str(v) for k, v in (kind.make.get("env") or {}).items()})
    return env


def make_version(cfg, kind, target, log=None):
    """Version vrnetlab's own Makefile derives from `target` (`make -s version-test`, no build, no file needed)."""
    bdir, vdir = prepare_work_copy(cfg, kind, f".vt-{os.getpid()}", light=True)
    try:
        p = run(["make", "-s", "-C", vdir, "version-test", f"IMAGE={target}"], log=log, env=_make_env(cfg, kind))
    finally:
        shutil.rmtree(bdir, ignore_errors=True)
    m = re.search(r"^Version:[ \t]*(.*)$", p.stdout or "", re.M)
    if p.returncode != 0 or not m or not m.group(1).strip():
        raise ClimgError(f"`make version-test IMAGE={target}` failed in {kind.dir}:\n{(p.stdout or '')[-500:]}")
    return m.group(1).strip()


def expected_tag(cfg, kind, target, log=None):
    """(tag, note): the Makefile's version wins over the predicted one."""
    ver = make_version(cfg, kind, target, log)
    pred = kind.predict_version(target)
    note = None if pred == ver else f"predicted version {pred!r} differs from make version-test {ver!r}"
    return f"{kind.repo}:{ver}", note


# ---------------------------------------------------------------- build

def build_image(cfg, kind, target, src_path, mode, log, tag=None, key=None, force=False):
    """Build one image. src_path is a plain disk file (mode 'plain') or an archive ('archive').
    Returns the final tag. `tag` overrides the name; the built tag never replaces an existing image
    unless force is set."""
    natural, note = expected_tag(cfg, kind, target, log)
    if note:
        log(f"warning: {note}; using the Makefile version")
    final = tag or natural
    existing = dockerx.inspect_tag(final)
    if existing and not force:
        log(f"no-op: {final} already present ({existing['Id'][:19]})")
        return final
    repo, ver = dockerx.split_tag(final)
    build_ver = ver if repo == kind.repo else f"climgtmp{os.getpid()}"
    lock = acquire_lock(cfg, f"build-{re.sub(r'[^A-Za-z0-9]+', '_', final)}", log)
    bdir = None
    t0 = time.time()
    try:
        if dockerx.inspect_tag(final) and not force:
            log(f"no-op: {final} became ready while waiting")
            return final
        slot = acquire_build_slot(cfg, log)  # noqa: F841 (held until return)
        disk_check(cfg, 2 * os.path.getsize(src_path) if mode == "plain" else 3 * os.path.getsize(src_path),
                   log, "before build")
        bdir, vdir = prepare_work_copy(cfg, kind, re.sub(r"[^\w.-]+", "_", final), log)
        applied = apply_patches(kind, bdir, log)
        extract(src_path, kind, os.path.join(vdir, target), log, mode)
        mk = kind.make
        cmd = ["make", mk["target"]] + [a.format(target=target) for a in mk["args"]]
        if applied:   # command-line variables override the Makefile's own DOCKER_BUILD_CMD
            cmd.append(f"DOCKER_BUILD_CMD=docker build --label climg.patches={patch_label(applied)}")
        if build_ver != natural.rsplit(":", 1)[1]:
            cmd.append(f"VERSION={build_ver}")
        log(f"state: building ({' '.join(cmd)}, cwd={vdir})")
        p = run(cmd, log=log, cwd=vdir, env=_make_env(cfg, kind))
        if p.returncode != 0:
            tail = "\n".join((p.stdout or "").splitlines()[-25:])
            raise ClimgError(f"make failed (rc={p.returncode}); tail:\n{tail}")
        built = f"{kind.repo}:{build_ver}"
        img = dockerx.inspect_tag(built)
        if not img:
            raise ClimgError(f"make succeeded but {built} is missing; see {log.path}")
        if built != final:
            run(["docker", "tag", built, final], log=log, check=True)
            run(["docker", "rmi", built], log=log, check=True)   # only the temporary tag created by this build
        img = dockerx.inspect_tag(final)
        log(f"state: ready  {final} id={img['Id'][:19]} size={gb(img['Size'])} in {int(time.time() - t0)} s")
        registry.record_build(cfg, final, {"key": key, "kind": kind.name, "target": target, "id": img["Id"],
                                           "patches": applied})
        return final
    finally:
        if bdir and os.path.exists(bdir):
            shutil.rmtree(bdir, ignore_errors=True)
        lock.close()


# ---------------------------------------------------------------- pull (LabHub)

def pull_labhub(cfg, inv, rec, log, force=False, tag=None):
    kind = inv.kinds.get(rec["kind"])
    if not kind or not rec.get("file"):
        raise ClimgError(f"{rec['key']} has no buildable kind/file")
    natural, note = expected_tag(cfg, kind, rec["target"], log)
    if note:
        log(f"warning: {note}")
    if not tag and dockerx.inspect_tag(natural) and not force:
        log(f"no-op: {natural} is already built")
        return natural
    if not tag and kind.family_match and not force and (inv.is_built(rec) is not None):
        log(f"no-op: a {kind.repo}:{rec['family']}-* image is already built")
        return natural
    f = rec["file"]
    lock = acquire_lock(cfg, "pull-" + f["checksum"]["md5"], log)
    try:
        src = inv.cached_source(rec)
        if src:
            log(f"source cached: {src}")
        else:
            disk_check(cfg, f["size"], log, "before download")
            src = download(cfg, inv.idx, f, log)
            disk_check(cfg, 3 * f["size"], log, "after download")
        return build_image(cfg, kind, rec["target"], src, rec["file_mode"], log, tag=tag, key=rec["key"],
                           force=force)
    finally:
        lock.close()


def docker_pull(ref, log):
    p = run(["docker", "pull", ref], log=log)
    if p.returncode != 0:
        raise ClimgError(f"docker pull {ref} failed: {(p.stdout or '').strip()[-300:]}")
