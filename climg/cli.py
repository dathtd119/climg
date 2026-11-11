"""climg command line."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

from . import CONTRACT_VERSION, __version__, build, dockerx, index as ix, job as jobmod, lab as labmod, nodetpl, \
    registry, setup, smoke, template, util
from .catalog import load_kinds
from .config import config_path, load_config
from .core import Inventory
from .util import (UA, Cancelled, ClimgError, Log, NeedsYes, emit_json, free_bytes, gb, hash_file, human, run,
                   progress, set_result, table, version_key, warn)

# exit codes (machine interface contract)
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_NEEDS_YES, EXIT_CANCELLED, EXIT_INTERRUPT = 0, 1, 2, 3, 4, 130
# stable error codes of the --json error envelope
ERROR_CODES = ("needs_yes", "not_found", "invalid_ref", "disk_floor", "locked", "no_config", "build_failed",
               "download_failed", "cancelled", "error")


def _need_yes(args, what):
    """Soft confirmation (lab --pull): report and carry on without acting."""
    if not args.yes:
        print(f"{what}\nRe-run with --yes to proceed (nothing was changed).")
        return False
    return True


def _confirm(args, what):
    """Hard confirmation: without --yes nothing changes and the command ends with exit status 3 (`needs_yes`)."""
    if not args.yes:
        raise NeedsYes(f"{what} Re-run with --yes to proceed (nothing was changed).",
                       f"{what}\nRe-run with --yes to proceed (nothing was changed).")


def _out(args, result):
    """Record the command's result object; with --json it is the one document on stdout."""
    set_result(result)
    if getattr(args, "json", False):
        emit_json(result)


def _tpl_name(kinds, tag):
    e = nodetpl.entry_for_tag(kinds, tag)
    return {"name": e["name"], "written": False, "entry": e} if e else None


def _public(rec):
    r = {k: v for k, v in rec.items() if k not in ("files", "file")}
    if rec.get("file"):
        r["file"] = {k: rec["file"][k] for k in ("filename", "path", "size", "checksum")}
    return r


# ------------------------------------------------------------------ index

def cmd_index(args, cfg, kinds):
    idx, meta = ix.load_index(cfg, refresh=args.refresh)
    recs = ix.build_records(idx, kinds)
    info = {"release": meta.get("release"), "age": ix.index_age(meta), "last_update": idx.get("last_update"),
            "schema_version": idx.get("schema_version"), "file": cfg.index_file, "source": meta.get("source"),
            "counts": {s: len(idx.get(s, [])) for s in ("QEMU", "IOL", "DYNAMIPS")},
            "buildable": sum(1 for r in recs if r["kind"]), "mirrors": cfg.mirrors}
    vendors = {}
    for r in recs:
        v = vendors.setdefault(r["vendor"], {"vendor": r["vendor"], "entries": 0, "buildable": 0})
        v["entries"] += 1
        v["buildable"] += 1 if r["kind"] else 0
    info["vendors"] = sorted(vendors.values(), key=lambda v: v["vendor"].lower())
    if args.json:
        return emit_json(info)
    print(f"release      {info['release']}  (index last_update {info['last_update']})")
    print(f"cached       {info['file']}  age {info['age']}  ttl {cfg.index_ttl / 3600:.0f}h")
    print("entries      " + "  ".join(f"{k}={v}" for k, v in info["counts"].items()) +
          "")
    print(f"buildable    {info['buildable']} entries map to a climg kind")
    print(f"vendors      {len(info['vendors'])} (see `climg index --json`; filter with `search --all --vendor V`)")
    print(f"mirrors      {', '.join(cfg.mirrors)}")


# ------------------------------------------------------------------ search

def _match_kw(rec, kw):
    text = " ".join(str(rec.get(k) or "") for k in ("name", "key", "kind", "version", "tag", "vendor", "folder")).lower()
    return all(w in text for w in kw.lower().split())


def _vendor_match(rec, kinds, wanted):
    """Case-insensitive match on the record's display vendor, or on the raw vendor of its kind (`paloalto`)."""
    w = wanted.strip().lower()
    k = kinds.get(rec["kind"]) if rec.get("kind") else None
    return w == (rec.get("vendor") or "").lower() or bool(k and w == k.vendor.lower())


def cmd_search(args, cfg, kinds):
    inv = Inventory(cfg, kinds, quiet=args.json)
    kind_filter = None
    if args.kind:
        k = kinds.find(args.kind)
        if not k:
            raise ClimgError(f"unknown kind {args.kind!r}")
        kind_filter = k.name
    rows = []
    for r in inv.all_records(include_unbuildable=args.all):
        if args.source != "all" and r["source"] != args.source:
            continue
        if kind_filter and r["kind"] != kind_filter:
            continue
        if args.vendor and not _vendor_match(r, kinds, args.vendor):
            continue
        if args.kw and not _match_kw(r, " ".join(args.kw)):
            continue
        st, det = inv.status(r)
        if args.status and st != args.status:
            continue
        rows.append((r, st, det))
    rows.sort(key=lambda x: (x[0]["kind"] or "~", x[0]["source"], version_key(x[0].get("version") or x[0]["name"])))
    if args.json:
        return emit_json([{**_public(r), "status": st, "detail": det} for r, st, det in rows])
    if not rows:
        print("no matches" + ("" if args.all else " (entries without a buildable kind are hidden; try --all)"))
        return 1
    print(table([(r["key"], r["kind"] or "-", r["version"] or "-", human(r["size"]), st, r["source"])
                 for r, st, _ in rows], ["KEY", "KIND", "VERSION", "SIZE", "STATUS", "SOURCE"]))
    nxt = next((r for r, st, _ in rows if st in ("available", "cached")), None)
    print(f"\n{len(rows)} entries.", end=" ")
    if nxt:
        print(f"next: climg pull {nxt['key']} --yes     (details: climg show {nxt['key']})")
    else:
        print(f"next: climg show {rows[0][0]['key']}")


# ------------------------------------------------------------------ show

def cmd_show(args, cfg, kinds):
    inv = Inventory(cfg, kinds, quiet=args.json)
    rec = inv.resolve(args.ref)
    st, det = inv.status(rec)
    kind = kinds.get(rec["kind"]) if rec.get("kind") else None
    make_ver = None
    if kind and rec.get("target") and cfg.vrnetlab and os.path.isdir(cfg.vrnetlab):
        try:
            make_ver = build.make_version(cfg, kind, rec["target"])
        except ClimgError as ex:
            make_ver = f"(version-test failed: {str(ex).splitlines()[0]})"
    tag = inv.built_tag(rec) if kind else rec.get("tag")
    creds = cfg.credentials if (cfg.credentials or {}).get("username") else {"username": "<user>", "password": "<pw>"}
    if args.json:   # machine output never carries the configured password
        creds = {"username": creds["username"], "password": util.REDACTED}
    snippet = None
    if kind and tag:
        snippet = template.render_topology(kind, tag, creds, rec.get("version") or "x", rec["key"])[1]["topology"]["kinds"]
    out = {**_public(rec), "status": st, "detail": det, "predicted_tag": rec.get("tag"), "built_tag": tag,
           "make_version_test": make_ver, "vrnetlab_dir": kind.dir if kind else None,
           "make": kind.make if kind else None, "files": rec.get("files"),
           "template_kinds_snippet": snippet,
           "patches": build.image_patches(inv.tags.get(tag)) if tag in inv.tags else None,
           "kind_patches": kind.patches if kind else None}
    if args.json:
        return emit_json(out)
    print(f"{rec['key']}   [{rec['source']}]")
    print(f"  name         {rec['name']}" + (f"   (LabHub id #{rec['id']}, {rec['section']})" if rec.get("id") else ""))
    print(f"  kind         {rec.get('kind') or '- (no buildable kind)'}   version {rec.get('version') or '-'}")
    if rec.get("path"):
        print(f"  file         {rec['path']}  ({human(rec.get('size'))})")
    for f in rec.get("files") or []:
        use = "  <- source" if rec.get("file") and f["filename"] == rec["file"]["filename"] else ""
        print(f"  file         {f['filename']}  {human(f['size'])}  md5 {f['checksum']['md5']}  "
              f"sha1 {f['checksum']['sha1']}{use}")
    if rec.get("sha1"):
        print(f"  sha1         {rec['sha1']}")
    if kind:
        print(f"  vrnetlab     {kind.dir}   make {kind.make['target']} {' '.join(kind.make['args'])}".rstrip())
        print(f"  target file  {rec.get('target')}")
        print(f"  docker tag   {rec.get('tag')} (predicted)" + (f"  make version-test -> {make_ver}" if make_ver else ""))
        if rec.get("tag") != tag:
            print(f"  built as     {tag}")
        if kind.patches:
            print(f"  patches      kind config: {', '.join(kind.patches)}")
        if out["patches"]:
            print(f"  patches      applied in {tag}: " + ", ".join(f"{p['name']} sha256 {p['sha256'][:12]}"
                                                                      for p in out["patches"]))
        if not kind.confirmed:
            print("  note         kind not smoke-tested by climg yet")
    print(f"  status       {st}" + (f"  ({det})" if det else ""))
    if snippet:
        print("  template     " + json.dumps(snippet))
        print(f"  next         climg template {rec['key']} -o ./tpl" +
              (f"   |   climg pull {rec['key']} --yes" if st in ("available", "cached") else
               f"   |   climg test {rec['key']}"))


# ------------------------------------------------------------------ images

def cmd_images(args, cfg, kinds):
    inv = Inventory(cfg, kinds, quiet=True)
    rows, notes = [], []
    for im in sorted(inv.images, key=lambda i: (i["tags"] or ["~"])[0]):
        vr_tag = any(t.startswith("vrnetlab/") for t in im["tags"])
        if not (im["vrnetlab"] or vr_tag):
            continue
        for tag in im["tags"] or [None]:
            kname = src = None
            note = []
            if tag:
                k = kinds.by_repo(dockerx.split_tag(tag)[0])
                kname = k.name if k else None
                s = inv.source_of(tag)
                src = s[0] if s[0] in ("climg", "external") else f"{s[0]} (not built by climg)"
                if s[0] == "external":
                    src = "external"
            else:
                note.append("DANGLING/untagged vrnetlab image (report only)")
                src = "unknown"
            if not im["vrnetlab"]:
                note.append("tagged vrnetlab/* but label+entrypoint rule does not match")
            patches = build.image_patches(im)
            if patches:
                note.append("patches: " + ", ".join(p["name"] for p in patches))
            rows.append({"tag": tag, "kind": kname, "source": src, "size": im["size"], "id": im["id"][7:19],
                         "built": bool(tag), "dangling": im["dangling"], "note": "; ".join(note),
                         "patches": patches})
    if args.json:
        return emit_json(rows)
    if not rows:
        print("no vrnetlab images found")
        return 0
    print(table([(r["tag"] or "<none>", r["kind"] or "-", r["source"], human(r["size"]), r["id"],
                  "DANGLING" if r["dangling"] else "BUILT", r["note"]) for r in rows],
                ["IMAGE", "KIND", "SOURCE", "SIZE", "ID", "STATE", "NOTE"]))
    dang = sum(1 for r in rows if r["dangling"])
    print(f"\n{len(rows) - dang} built, {dang} dangling (nothing is deleted by climg).")


# ------------------------------------------------------------------ lab

def cmd_lab(args, cfg, kinds):
    paths = args.paths or cfg.lab_roots
    if not paths:
        raise ClimgError("give a topology file/dir (or set lab_roots in the config)")
    inv = Inventory(cfg, kinds, quiet=args.json)
    topos = []
    for p in paths:
        for f in labmod.find_topologies(p):
            t = labmod.parse_topology(f)
            if t:
                topos.append(t)
    if not topos:
        if args.json:
            raise ClimgError("no containerlab topologies found", "not_found")
        print("no containerlab topologies found")
        return 1
    cache, results = {}, []
    for t in topos:
        for n in t["nodes"]:
            key = n["image"]
            if key not in cache:
                cache[key] = labmod.classify(inv, n["image"], n["kind"])
            results.append({"file": t["file"], "lab": t["name"], "node": n["node"], "kind": n["kind"],
                            "image": n["image"], **cache[key]})
    actions = []
    if args.pull:
        todo = {}
        for r in results:
            if r["state"] in (labmod.CACHED, labmod.PULLABLE, labmod.VENDOR, labmod.REGISTRY):
                todo.setdefault(r["image"], r)
        if todo and _need_yes(args, "Would fetch/build: " + ", ".join(sorted(todo))):
            for image, r in sorted(todo.items()):
                log = Log(cfg, "lab")
                try:
                    if r["state"] == labmod.REGISTRY:
                        build.docker_pull(r["ref"], log)
                    else:
                        rec = inv.resolve(r["ref"])
                        if rec["source"] == "local":
                            build.build_image(cfg, kinds.get(rec["kind"]), rec["target"], rec["path"], "plain", log,
                                              key=rec["key"])
                        else:
                            build.pull_labhub(cfg, inv, rec, log)
                    actions.append((image, "ok"))
                except ClimgError as ex:
                    actions.append((image, f"FAILED: {str(ex).splitlines()[0]}"))
                    log(f"lab pull {image} failed: {ex}", echo=False)
        if todo and args.yes:
            inv = Inventory(cfg, kinds, quiet=True)
            cache.clear()
            for r in results:
                r.update(cache.setdefault(r["image"], labmod.classify(inv, r["image"], r["kind"])))
    missing = [r for r in results if r["state"] == labmod.MISSING]
    if args.json:
        emit_json({"nodes": results, "actions": actions, "missing": len(missing)})
    else:
        for t in topos:
            print(f"== {t['file']}  (lab {t['name']})")
        print()
        print(table([(os.path.basename(r["file"]), r["node"], r["kind"] or "-", r["image"] or "-", r["state"])
                     for r in results], ["TOPOLOGY", "NODE", "KIND", "IMAGE", "STATE"]))
        hints = {}
        for r in results:
            if r["state"] != labmod.BUILT and r["hint"]:
                hints.setdefault(r["image"], (r["state"], r["hint"]))
        if hints:
            print("\nnext:")
            for image, (state, hint) in hints.items():
                print(f"  {state:<17} {image}: {hint}")
        for image, res in actions:
            print(f"pull {image}: {res}")
        print(f"\n{len(results)} nodes in {len(topos)} topologies; {len(missing)} MISSING.")
    return 1 if missing else 0


# ------------------------------------------------------------------ pull

def _build_local(cfg, inv, rec, log, force, tag=None):
    return build.build_image(cfg, inv.kinds.get(rec["kind"]), rec["target"], rec["path"], "plain", log,
                             tag=tag, key=rec["key"], force=force)


def cmd_pull(args, cfg, kinds):
    inv = Inventory(cfg, kinds)
    rec = inv.resolve(args.ref)
    st, det = inv.status(rec)
    if st == "built" and not args.force and not args.tag:
        print(f"no-op: {rec['key']} is already built as {det}")
        return _out(args, {"ref": rec["key"], "status": "noop", "tag": det, "nodetpl": _tpl_name(kinds, det)}) or 0
    if not rec.get("kind"):
        raise ClimgError(f"{rec['key']} has no buildable kind; see `climg search --all`", "not_found")
    if rec["source"] == "docker":
        raise ClimgError(f"{rec['key']} is a docker image with no known source", "not_found")
    kind = kinds.get(rec["kind"])
    if rec["source"] == "labhub":
        have = inv.cached_source(rec)
        what = (f"build {rec['tag']} from cached {have}" if have else
                f"download {human(rec['size'])} then build {rec['tag']}")
    else:
        what = f"build {rec['tag']} from {rec['path']}"
    _confirm(args, f"Would {what} ({kind.dir}, work copy under {cfg.work_dir}).")
    log = Log(cfg, rec["name"])
    tag = (build.pull_labhub(cfg, inv, rec, log, args.force, tag=args.tag) if rec["source"] == "labhub"
           else _build_local(cfg, inv, rec, log, args.force, tag=args.tag))
    print(f"done: {tag}   next: climg template {rec['key']} -o ./tpl   |   climg test {rec['key']}")
    tpl = nodetpl.after_build(cfg, kinds, tag, write=not args.no_nodetpl)
    _out(args, {"ref": rec["key"], "status": "built", "tag": tag, "nodetpl": tpl})


# ------------------------------------------------------------------ add

def cmd_build(args, cfg, kinds):
    """add + build: a file path (registered first) or a `local:<key>` ref from scan/search."""
    ref = args.ref
    if ref.startswith("local:") and not os.path.exists(ref):
        inv = Inventory(cfg, kinds, need_index=False, quiet=True)
        rec = inv.resolve(ref)
        if inv.is_built(rec):
            tag = inv.built_tag(rec)
            print(f"no-op: {rec['key']} is already built as {tag}")
            return _out(args, {"ref": rec["key"], "status": "noop", "tag": tag, "nodetpl": _tpl_name(kinds, tag)}) or 0
        _confirm(args, f"Would build {rec['tag']} from {rec['path']}.")
        tag = _build_local(cfg, inv, rec, Log(cfg, rec["name"]), False)
        print(f"built {tag}")
        tpl = nodetpl.after_build(cfg, kinds, tag, write=not args.no_nodetpl)
        _out(args, {"ref": rec["key"], "status": "built", "tag": tag, "nodetpl": tpl})
        return 0
    args.build = True
    args.file = ref
    return cmd_add(args, cfg, kinds)


def cmd_add(args, cfg, kinds):
    path = os.path.realpath(os.path.expanduser(args.file))
    if not os.path.isfile(path):
        raise ClimgError(f"{args.file}: not a file", "not_found")
    fn = os.path.basename(path)
    rec = registry.local_record(kinds, fn, path, kind=args.kind, ver=args.version)
    if rec is None:
        ex = "; ".join(f"{k.name}: {k.example}" for k in kinds if k.example)
        raise ClimgError(f"cannot detect kind/version from {fn!r}. Pass --kind K --version V. Known names: {ex}",
                         "invalid_ref")
    kind = kinds.get(rec["kind"])
    if os.path.splitext(fn)[1].lower() not in kind.image_ext:
        warn(f"{fn}: extension is not one of {kind.image_ext} for {kind.name}; vrnetlab may reject it")
    print(f"detected: kind {kind.name}  version {rec['version']}  -> vrnetlab file {rec['target']}")
    try:
        tag, note = build.expected_tag(cfg, kind, rec["target"])
        print(f"make version-test: {tag}" + (f"   WARNING: {note}" if note else "   (matches prediction)"))
        rec["tag"] = tag
    except ClimgError as ex:
        warn(f"could not confirm with make version-test: {str(ex).splitlines()[0]}")
    print("hashing ...", flush=True)
    registry.hashed(rec)
    if args.tag:
        rec["extra_tags"] = [args.tag]
    registry.register(cfg, rec)
    print(f"registered {rec['key']}  size {human(rec['size'])}  sha1 {rec['sha1']}  (original file kept at {path})")
    result = {"ref": rec["key"], "status": "registered", "kind": kind.name, "version": rec["version"],
              "tag": rec["tag"], "path": path, "size": rec["size"], "sha1": rec["sha1"], "nodetpl": None}
    if args.build:
        final = args.tag or rec["tag"]
        _confirm(args, f"Would build {final} from {path}.")
        log = Log(cfg, rec["name"])
        done = build.build_image(cfg, kind, rec["target"], path, "plain", log, tag=args.tag, key=rec["key"])
        print(f"built {done}")
        result.update(status="built", tag=done,
                      nodetpl=nodetpl.after_build(cfg, kinds, done, write=not args.no_nodetpl))
    else:
        print(f"next: climg pull {rec['key']} --yes")
    _out(args, result)


# ------------------------------------------------------------------ template

def cmd_template(args, cfg, kinds):
    creds = cfg.need_credentials()
    if args.json:   # machine output never carries the configured password
        creds = {"username": creds["username"], "password": util.REDACTED}
    inv = Inventory(cfg, kinds, need_index=True, quiet=True)
    rec = inv.resolve(args.ref)
    if not rec.get("kind"):
        raise ClimgError(f"{rec['key']} has no buildable kind", "not_found")
    kind = kinds.get(rec["kind"])
    tag = inv.built_tag(rec)
    ver = rec.get("version") or "x"
    st, _ = inv.status(rec)
    if st != "built":
        warn(f"{tag} is not built yet (run `climg pull {rec['key']} --yes`); template uses the predicted tag")
    wrote = []
    if args.json and not args.output and not args.ui:   # a program asked for the content: write nothing
        text, doc = template.render_topology(kind, tag, creds, ver, rec["key"])
        return _out(args, {"ref": rec["key"], "tag": tag, "built": st == "built", "wrote": [],
                           "topology": doc, "text": text}) or 0
    if args.output or not args.ui:
        out_dir = args.output or "."
        os.makedirs(out_dir, exist_ok=True)
        text, _doc = template.render_topology(kind, tag, creds, ver, rec["key"])
        fn = os.path.join(out_dir, re.sub(r"[^\w.-]+", "_", f"{kind.alias}-{ver}") + ".clab.yml")
        with open(fn, "w") as f:
            f.write(text)
        wrote.append(fn)
    if args.ui:
        added, updated = template.merge_ui(args.ui, [template.ui_entry(kind, tag, ver)])
        wrote.append(f"{args.ui} (+{added} new, {updated} updated)")
    for w in wrote:
        print(f"wrote {w}")
    if wrote and wrote[0].endswith(".clab.yml"):
        print(f"validate: containerlab validate -t {wrote[0]}")
    _out(args, {"ref": rec["key"], "tag": tag, "built": st == "built", "wrote": wrote, "topology": None})


# ------------------------------------------------------------------ scan

def cmd_scan(args, cfg, kinds):
    inv = Inventory(cfg, kinds, need_index=False, quiet=True)
    rows, js = [], []
    for f in registry.scan_files(cfg, kinds):
        rec = f["rec"]
        if rec:
            st, det = inv.status(rec)
            status = "BUILT" if st == "built" else "IMPORTED (not built)"
            tag = inv.built_tag(rec) if st == "built" else rec["tag"]
            kind, ver = rec["kind"], rec["version"]
        else:
            status, tag, kind, ver = "UNKNOWN (no kind rule; use `climg add --kind K --version V`)", None, None, None
        js.append({"path": f["path"], "size": f["size"], "kind": kind, "version": ver, "tag": tag, "status": status})
        rows.append((f["filename"], kind or "-", ver or "-", tag or "-", human(f["size"]), status))
    if args.json:
        return emit_json(js)
    print("images dirs: " + ", ".join(cfg.images_dirs))
    if not rows:
        print("no image files (*.qcow2 *.vmdk *.tgz *.tar.gz *.bin *.iol) found")
        return 0
    print(table(rows, ["FILE", "KIND", "VERSION", "TAG", "SIZE", "STATUS"]))
    todo = [r for r in js if r["status"].startswith("IMPORTED")]
    if todo:
        print(f"\nnext: climg build local:{os.path.basename(todo[0]['path'])} --yes")


# ------------------------------------------------------------------ init

def cmd_init(args, cfg, kinds):
    path = args.config or config_path()
    existing = setup.read_existing(path)
    new, notes = setup.detect({"workspace": args.workspace, "images_dir": args.images_dir,
                               "vrnetlab": args.vrnetlab, "app_container": args.app_container}, existing)
    print(f"config file: {path}" + ("  (exists; updated in place)" if os.path.exists(path) else ""))
    for n in notes:
        print("  " + n)
    if not args.yes:
        if args.json or not sys.stdin.isatty():
            raise NeedsYes("init needs --yes when it cannot ask (stdin is not a terminal or --json is given)",
                           "Not interactive: re-run with --yes to write this config (nothing was changed).")
        if input("Write this config and create the directories? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted (nothing was changed)")
            return 1
    setup.write_config(path, new)
    made = setup.make_dirs(new)
    print(f"wrote {path}")
    for d in made:
        print(f"created {d}")
    print("next: climg scan   |   climg nodetpl sync --dry-run")
    _out(args, {"config": path, "created": made})


# ------------------------------------------------------------------ nodetpl

def _writer(args, cfg):
    return nodetpl.pick_writer(cfg, getattr(args, "writer", None))


def _tpl_result(action, changes, skipped, via, dry):
    names = lambda *acts: [n for a, n, _ in changes if a in acts]  # noqa: E731
    return {"action": action, "dry_run": dry, "writer": via,
            "changes": [{"action": a, "name": n, **({"kept": k} if a == "updated" else {})} for a, n, k in changes],
            "skipped": [{"name": n, "reason": why} for n, why in skipped],
            "added": names("created"), "updated": names("updated"), "removed": names("deleted")}


def _report(changes, skipped, via, dry):
    pre = "would " if dry else ""
    for action, name, kept in changes:
        print(f"{pre}{action:<8} {name}" + (f" (kept your {', '.join(kept)})" if kept else ""))
    for name, why in skipped:
        print(f"skipped  {name}: {why}")
    if not changes:
        print("nothing to change")
    print(f"({len(changes)} change(s), {len(skipped)} skipped, writer: {via})")


def cmd_nodetpl(args, cfg, kinds):
    w = _writer(args, cfg)
    if args.action == "list":
        cur, owned, via = nodetpl.list_templates(cfg, w)
        gen, regen = nodetpl.load_generated(cfg), nodetpl.regen_for(kinds)
        custom = {c.get("name") for c in cur if c.get("name") in owned
                  and nodetpl.is_customized(c, gen.get(c.get("name")), regen)}
        rows = [(("*" if c.get("name") in owned else " "), c.get("name"), c.get("kind"), c.get("image", "-"),
                 c.get("icon", "-")) for c in cur]
        if args.json:
            return emit_json([{"name": r[1], "kind": r[2], "image": r[3], "icon": r[4], "owned": r[0] == "*",
                               "customized": r[1] in custom}
                              for r in rows])
        print(table(rows, ["OWN", "NAME", "KIND", "IMAGE", "ICON"]))
        print(f"\n{len(rows)} templates, {sum(1 for r in rows if r[0] == '*')} owned by climg (*); writer: {via}")
        return 0
    if args.action == "sync":
        ops = nodetpl.sync_ops(cfg, kinds)
        progress("template", None, "syncing node templates")
        res = nodetpl.apply_ops(cfg, ops, w, dry_run=args.dry_run, kinds=kinds)
        _report(*res, args.dry_run)
        _out(args, _tpl_result("sync", *res, args.dry_run))
        return 0
    if args.action == "add":
        inv = Inventory(cfg, kinds, quiet=True)
        rec = inv.resolve(args.target)
        if not rec.get("kind"):
            raise ClimgError(f"{rec['key']} has no buildable kind", "not_found")
        st, _ = inv.status(rec)
        if st != "built":
            raise ClimgError(f"{rec['key']} is not built yet (status {st}); `climg pull {rec['key']} --yes` first",
                             "not_found")
        e = nodetpl.entry_for_tag(kinds, inv.built_tag(rec))
        res = nodetpl.apply_ops(cfg, [("upsert", e)], w, kinds=kinds)
        _report(*res, False)
        _out(args, {**_tpl_result("add", *res, False), "template": e})
        return 0
    if args.action == "remove":
        res = nodetpl.apply_ops(cfg, [("delete", args.target)], w)
        _report(*res, False)
        _out(args, _tpl_result("remove", *res, False))
        return 0


# ------------------------------------------------------------------ config

def cmd_config(args, cfg, kinds):
    """Effective configuration for programs; credential values are never included."""
    info = {"contract_version": CONTRACT_VERSION, "version": __version__, "config_file": cfg.path,
            "config_exists": cfg.exists, "state_dir": cfg.state, "images_dirs": cfg.images_dirs,
            "disk_floor_gb": cfg.disk_floor / 1e9, "disk_floor_bytes": cfg.disk_floor, "max_builds": cfg.max_builds,
            "mirrors": cfg.mirrors, "index_ttl_hours": cfg.index_ttl / 3600, "vrnetlab": cfg.vrnetlab,
            "workspace": cfg.workspace, "nodetpl_file": cfg.nodetpl_file, "nodetpl_writer": cfg.nodetpl_writer,
            "nodetpl_url": cfg.nodetpl_url, "credentials_set": bool((cfg.credentials or {}).get("password"))}
    if args.json:
        return emit_json(info)
    for k, v in info.items():
        print(f"{k:<16} {v}")


# ------------------------------------------------------------------ doctor

def _probe_mirror(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, int((time.time() - t0) * 1000)
    except Exception as ex:
        return getattr(ex, "code", repr(ex)), None


def _mem_available():
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemAvailable:"):
                    return int(ln.split()[1]) * 1024
    except OSError:
        pass
    return None


def cmd_doctor(args, cfg, kinds):
    checks = []

    def add(name, ok, detail):
        checks.append((name, "ok" if ok else "FAIL", detail))

    add("config", True, cfg.path + ("" if os.path.exists(cfg.path or "") else "  (absent: defaults in use)"))
    add("credentials", bool((cfg.credentials or {}).get("username")),
        "set" if cfg.credentials else "not set (needed by template/test)")
    add("state dir", os.access(cfg.state, os.W_OK) if os.path.isdir(cfg.state) else True, cfg.state)
    for d in cfg.extra_cache_dirs:
        add("extra cache", os.path.isdir(d), d)
    for d in cfg.images_dirs:
        add("images dir", True, d + ("" if os.path.isdir(d) else "  (absent: run `climg init`)"))
    idx = None
    try:
        idx, meta = ix.load_index(cfg, quiet=True)
        add("labhub index", True, f"{meta.get('release')} age {ix.index_age(meta)}")
    except ClimgError as ex:
        add("labhub index", False, str(ex))
    try:
        req = urllib.request.Request(ix.RELEASE_API, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            add("github api", True, f"HTTP {r.status} {ix.RELEASE_API}")
    except Exception as ex:
        add("github api", False, repr(ex))
    if idx:
        small = min((f for e in idx["QEMU"] for f in e["files"] if f["size"] > 1000), key=lambda f: f["size"])
        for key in cfg.mirrors:
            url = ix.mirror_urls(idx, small["path"], [key])
            if not url:
                add(f"mirror {key}", False, "not defined in index url_properties")
                continue
            code, ms = _probe_mirror(url[0])
            add(f"mirror {key}", code == 206, f"Range probe HTTP {code}" + (f" in {ms} ms" if ms else "") +
                f"  {url[0].split('/')[2]}")
    p = run(["docker", "version", "--format", "{{.Server.Version}}"]) if shutil.which("docker") else None
    add("docker", bool(p and p.returncode == 0), (p.stdout.strip() if p else "not installed")[:100])
    if not os.path.exists("/dev/kvm"):
        add("/dev/kvm", False, "missing")
    elif os.access("/dev/kvm", os.R_OK | os.W_OK):
        add("/dev/kvm", True, "present and accessible")
    else:   # vrnetlab containers run privileged, so this user needs no access of its own
        checks.append(("/dev/kvm", "warn", "present, not accessible to this user (privileged containers still work)"))
    p = run(["containerlab", "version"]) if shutil.which("containerlab") else None
    m = re.search(r"version:\s*(\S+)", p.stdout if p else "")
    add("containerlab", bool(m), m.group(1) if m else "not found")
    vr = cfg.vrnetlab
    if vr and os.path.isdir(os.path.join(vr, "common")):
        g = run(["git", "-C", vr, "rev-parse", "--short", "HEAD"])
        missing = [k.dir for k in kinds if not os.path.isfile(os.path.join(vr, k.dir, "Makefile"))]
        add("vrnetlab", True, f"{vr} @ {g.stdout.strip()}" + (f"; kinds without a platform dir: {', '.join(missing)}"
                                                           if missing else ""))
    else:
        add("vrnetlab", False, f"checkout not found: {vr!r}")
    add("make", bool(shutil.which("make")), shutil.which("make") or "not found")
    free = free_bytes(cfg.state)
    add("disk", free > cfg.disk_floor, f"{gb(free)} free, floor {gb(cfg.disk_floor)}")
    mem = _mem_available()
    add("memory", mem is not None, f"{gb(mem)} available" if mem else "unknown")
    try:
        import pexpect  # noqa: F401
        add("pexpect", True, "importable (needed by `climg test`)")
    except ImportError:
        add("pexpect", False, "missing (needed by `climg test`)")
    if args.json:
        emit_json([{"check": n, "status": s, "detail": d} for n, s, d in checks])
    else:
        print(table(checks, ["CHECK", "STATUS", "DETAIL"]))
    return 1 if any(s == "FAIL" for _, s, _ in checks if _ not in ("pexpect",)) else 0


# ------------------------------------------------------------------ test

def cmd_test(args, cfg, kinds):
    inv = Inventory(cfg, kinds, quiet=True)
    rec = inv.resolve(args.ref)
    if not rec.get("kind"):
        raise ClimgError(f"{rec['key']} has no buildable kind")
    kind = kinds.get(rec["kind"])
    tag = inv.built_tag(rec)
    need = (kind.resources["ram_mb"] if kind.qemu_env else 2048) * 1024 * 1024
    mem = _mem_available()
    if mem is not None and mem < need + 2 * 1024 ** 3 and not args.force:
        raise ClimgError(f"only {gb(mem)} RAM available; {kind.name} wants {gb(need)} + 2 GB headroom "
                         f"(--force to ignore)")
    res = smoke.run_test(cfg, kind, tag, rec["key"], args.budget)
    if args.json:
        emit_json(res)
    else:
        print(f"{res['result']}  {tag}  boot {res['boot_s']}s  stages {res['stages']}" +
              (f"  match: {res['show_match']}" if res["show_match"] else "") +
              (f"  {res['fail_class']}: {res['diagnosis']}" if res["fail_class"] else ""))
        print(f"run dir: {res['run_dir']}")
    return 0 if res["result"] == "PASS" else 1


# ------------------------------------------------------------------ main

def make_parser():
    ap = argparse.ArgumentParser(prog="climg", description="containerlab image manager "
                                 "(LabHub search/pull -> vrnetlab build -> containerlab templates)")
    ap.add_argument("--config", help="config file (default ~/.config/climg/config.yaml)")
    ap.add_argument("--version", action="version", version=f"climg {__version__} (contract {CONTRACT_VERSION})")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name, fn, help_):
        p = sp.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("index", cmd_index, "show/refresh the cached LabHub index")
    p.add_argument("--refresh", action="store_true"); p.add_argument("--json", action="store_true")
    p = add("search", cmd_search, "search LabHub + local vendor images")
    p.add_argument("kw", nargs="*"); p.add_argument("--kind"); p.add_argument("--vendor")
    p.add_argument("--source", choices=["labhub", "local", "all"], default="all")
    p.add_argument("--status", choices=["built", "cached", "available"])
    p.add_argument("--all", action="store_true", help="include entries without a buildable kind")
    p.add_argument("--json", action="store_true")
    p = add("show", cmd_show, "everything about one image")
    p.add_argument("ref"); p.add_argument("--json", action="store_true")
    p = add("images", cmd_images, "built vrnetlab images joined with the registry")
    p.add_argument("--json", action="store_true")
    p = add("lab", cmd_lab, "check (and optionally fetch) the images a topology needs")
    p.add_argument("paths", nargs="*"); p.add_argument("--pull", action="store_true")
    p.add_argument("--yes", action="store_true"); p.add_argument("--json", action="store_true")
    def machine(p, job=False, nodetpl_step=False):
        p.add_argument("--json", action="store_true", help="one JSON document on stdout; progress/warnings on stderr")
        if job:
            p.add_argument("--job-file", metavar="PATH", help="atomically rewritten JSON progress file")
        if nodetpl_step:
            p.add_argument("--no-nodetpl", action="store_true", help="skip the Node Template write")

    p = add("pull", cmd_pull, "download, extract and build an image")
    machine(p, True, True)
    p.add_argument("ref"); p.add_argument("--yes", action="store_true"); p.add_argument("--force", action="store_true")
    p.add_argument("--tag", help="build under this tag instead of the natural one (e.g. a temporary test tag)")
    p = add("add", cmd_add, "register a vendor-supplied image file")
    machine(p, True, True)
    p.add_argument("file"); p.add_argument("--kind"); p.add_argument("--version"); p.add_argument("--tag")
    p.add_argument("--build", action="store_true"); p.add_argument("--yes", action="store_true")
    p = add("template", cmd_template, "write a 2-node .clab.yml and/or merge a UI node template")
    machine(p)
    p.add_argument("ref"); p.add_argument("-o", "--output", metavar="DIR"); p.add_argument("--ui", metavar="JSON")
    p = add("init", cmd_init, "detect workspace/app/images dir, write the config, create the folders")
    machine(p)
    p.add_argument("--workspace"); p.add_argument("--images-dir"); p.add_argument("--vrnetlab")
    p.add_argument("--app-container"); p.add_argument("--yes", action="store_true")
    p = add("scan", cmd_scan, "list image files in the images dir(s) with kind/version/tag/status")
    p.add_argument("--json", action="store_true")
    p = add("build", cmd_build, "register (add) and build a vendor image: a file or local:<key>")
    machine(p, True, True)
    p.add_argument("ref"); p.add_argument("--kind"); p.add_argument("--version"); p.add_argument("--tag")
    p.add_argument("--yes", action="store_true")
    p = add("nodetpl", cmd_nodetpl, "manage the containerlab-app Node Templates climg owns")
    p.add_argument("action", choices=["list", "sync", "add", "remove"])
    p.add_argument("target", nargs="?", help="add: ref or image; remove: template name")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--json", action="store_true")
    p.add_argument("--writer", choices=["file", "rest"], help="override the configured writer (default auto)")
    p.add_argument("--nodetpl-file", metavar="PATH", help="Node Template file to use (or CLIMG_NODETPL_FILE)")
    p.add_argument("--job-file", metavar="PATH", help="atomically rewritten JSON progress file (sync)")
    p = add("config", cmd_config, "print the effective configuration (no credential values)")
    p.add_argument("--json", action="store_true")
    p = add("doctor", cmd_doctor, "check mirrors, docker, kvm, containerlab, vrnetlab, disk, config")
    p.add_argument("--json", action="store_true")
    p = add("test", cmd_test, "smoke test: deploy one node, SSH login, show version, destroy")
    p.add_argument("ref"); p.add_argument("--budget", type=int, help="boot budget seconds")
    p.add_argument("--force", action="store_true", help="ignore the free-RAM guard"); p.add_argument("--json", action="store_true")
    return ap


NO_CONFIG_OK = ('init', 'doctor', 'config')


def _error_doc(code, message):
    return {"error": {"code": code, "message": message}}


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)     # a usage error exits 2 here (argparse), nowhere else
    if args.cmd == "nodetpl" and args.action == "list" and args.target:
        parser.error("nodetpl list takes no target")   # py3.13 argparse accepts the extra positional; 3.12 did not
    as_json = getattr(args, "json", False)
    saved = (sys.stdout, sys.stdin, signal.getsignal(signal.SIGTERM))
    job = None
    util.JSON_OUT = util.LOG_HOOK = util.PROGRESS_HOOK = util.LAST_RESULT = None
    util.SECRETS = []

    def on_term(signum, frame):
        raise Cancelled()

    signal.signal(signal.SIGTERM, on_term)
    if as_json:
        util.JSON_OUT, sys.stdout = sys.stdout, sys.stderr    # stdout carries the one JSON document only
    if as_json or getattr(args, "yes", False):
        sys.stdin = open(os.devnull)                           # never block on, or read, stdin
    rc, state, doc, msg = EXIT_OK, "succeeded", None, ""
    try:
        try:
            cfg = None if args.cmd == "init" else load_config(args.config)   # init rewrites a stale config
            if cfg and not cfg.exists and args.cmd not in NO_CONFIG_OK:
                raise ClimgError("no config: run `climg init`", "no_config")
            if cfg:
                util.SECRETS = [str(cfg.credentials["password"])] if (
                    isinstance(cfg.credentials, dict) and cfg.credentials.get("password")) else []
                tpl_file = getattr(args, "nodetpl_file", None) or os.environ.get("CLIMG_NODETPL_FILE")
                if tpl_file:
                    cfg.nodetpl_file = os.path.expanduser(tpl_file)
            if getattr(args, "job_file", None):
                job = jobmod.JobFile(args.job_file, "template" if args.cmd == "nodetpl" else "download")
                util.LOG_HOOK, util.PROGRESS_HOOK = job.log_line, job.progress
            if args.cmd in ("nodetpl",) and args.action in ("add", "remove") and not args.target:
                raise ClimgError(f"nodetpl {args.action} needs a target", "invalid_ref")
            rc = args.fn(args, cfg, load_kinds()) or 0
            if rc:
                state = "failed"
        except NeedsYes as ex:
            rc, state, msg, doc = EXIT_NEEDS_YES, "failed", str(ex), _error_doc(ex.code, str(ex))
            if as_json:
                print(f"error: {ex}", file=sys.stderr)
            else:
                print(ex.text)
        except ClimgError as ex:
            rc, state, msg, doc = EXIT_FAIL, "failed", str(ex), _error_doc(ex.code, str(ex))
            print(f"error: {ex}", file=sys.stderr)
        except Cancelled:
            rc, state, msg, doc = EXIT_CANCELLED, "cancelled", "cancelled", _error_doc("cancelled", "cancelled")
            print("cancelled", file=sys.stderr)
        except KeyboardInterrupt:
            rc, state, msg, doc = EXIT_INTERRUPT, "cancelled", "interrupted", _error_doc("cancelled", "interrupted")
            print("interrupted", file=sys.stderr)
        except Exception as ex:
            if not as_json:
                raise
            rc, state, msg = EXIT_FAIL, "failed", f"{type(ex).__name__}: {ex}"
            doc = _error_doc("internal", msg)
            print(f"error: {msg}", file=sys.stderr)
        if doc is not None and as_json:
            emit_json(doc)
        if job:
            job.finish(state, util.redact(doc if doc is not None else util.LAST_RESULT), msg)
        return rc
    finally:
        if sys.stdin is not saved[1]:
            sys.stdin.close()
        sys.stdout, sys.stdin = saved[0], saved[1]
        signal.signal(signal.SIGTERM, saved[2])
        util.JSON_OUT = util.LOG_HOOK = util.PROGRESS_HOOK = None
