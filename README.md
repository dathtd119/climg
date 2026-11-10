# climg - containerlab image manager

`climg` is modeled on [ishare2-cli](https://github.com/ishare2-org/ishare2-cli) (search / pull / installed / labs for
EVE-NG and PNETLab) but native to [containerlab](https://containerlab.dev): it finds network-OS images on the LabHub
mirrors (the ishare2 index), builds them into docker images with [srl-labs/vrnetlab](https://github.com/srl-labs/vrnetlab),
checks which images a topology needs, and writes ready-to-validate templates.

Python 3.12, stdlib + PyYAML only (`pexpect` is imported lazily and only by `climg test`).
Every command is non-interactive (except the confirmation of `init`); anything that downloads or builds needs `--yes`.
First run `climg init`: every command except `init`, `doctor` and `--help` fails with `no config: run \`climg init\`` until the config exists.

## Install

```
git clone <this repo> ~/code/climg          # or rsync the directory
~/code/climg/bin/climg --help               # no build step; optional: ln -s ~/code/climg/bin/climg ~/.local/bin/climg
python3 -m unittest discover -s tests       # self-check
```

Host requirements: python3.12 with PyYAML, `make`, docker, containerlab, an up-to-date vrnetlab checkout
(never modified: builds run in a private work copy), and `pexpect` + an `ssh` client for `climg test`.
No sudo, pip or uv is needed.

## Config

`~/.config/climg/config.yaml` (override with `--config FILE` or `$CLIMG_CONFIG`). Unknown keys are rejected.

```yaml
vrnetlab: ~/vrnetlab                               # vrnetlab checkout (read only)
state_dir: ~/.local/share/climg                  # index cache, downloads, registry, locks, logs, results.jsonl
extra_cache_dirs:                                # read-only caches laid out as <dir>/<md5>/<filename>
  - /srv/images/cache
images_dirs:                                     # hand-imported vendor images (default: <state>/images)
  - ~/.local/share/climg/images
workspace: ~/.clab                                # containerlab workspace (written by init)
app_container: containerlab-app                  # running containerlab-app, if any
nodetpl_file: ~/.clab/ui/custom-nodes.json            # host path of the app's Node Template file; absent = REST only
nodetpl_url: https://127.0.0.1:3001              # app base URL for the REST writer
nodetpl_writer: auto                             # auto (REST if reachable, else file) | file | rest
mirrors: [main, drive, legacy]                   # LabHub hostname keys in failover order ("secondary" redirects: skip)
disk_floor_gb: 15                                # checked before and after a download and before a build
max_builds: 2                                    # concurrent builds (flock slots)
index_ttl_hours: 168
index_pinned_release: LabHub-Indexes-2.0.1
index_fallback_files: []                         # offline copies of labhub.json
credentials: {username: admin, password: <lab-default>}   # lab default login, used by template/test; set it in your own config
lab_roots: [~/.clab]                                # default [workspace]; scanned by `climg lab` without arguments
```

### Folder layout

| Path | Owner | Content |
|---|---|---|
| `~/.config/climg/config.yaml` | climg | config |
| `~/.local/share/climg/` | climg | state: index, downloads, registry, builds, locks, logs, runs, `nodetpl-owned.json` |
| `~/.local/share/climg/images/` | you | hand-imported vendor images (`--images-dir`, list allowed in `images_dirs`) |
| `~/.clab/` (or the app's `WEB_LOCAL_WORKSPACE`) | containerlab / web app | labs; the file browser of the web app lists every folder in it, so **climg stores nothing here except `ui/custom-nodes.json`** (the Node Templates) |

State layout under `state_dir`: `index/labhub.json` (+`meta.json`), `downloads/<md5>/<file>` (resumable `.part`),
`registry.json` (vendor images), `builds.json` (images built by climg), `work/` (build copies, removed after each build),
`locks/`, `logs/`, `runs/` (smoke-test artifacts), `results.jsonl`.

`kinds.yaml` (next to the code) is the data file: per containerlab kind the vrnetlab directory, make target/args,
LabHub name patterns, vendor filename patterns, VM resources, interface names, login prompt and probe. Adding a vendor
file whose name no pattern recognizes needs no code: `climg add FILE --kind K --version V`.

## Commands

Refs accepted by `show/pull/template/test`: `labhub:<name>[@sha1prefix]`, `local:<name>`, a kind alias (`vsrx`, `csr`,
`c8kv`, `n9kv`, `xrv9k`, `vios`, `fgt`, `pan`, ...), a docker tag, `#<id>` (positional id of the current index), or
`<alias> <version>`. Status values: `built` (docker image present), `cached` (source file already on disk), `available`
(downloadable from LabHub). `--json` exists on the read commands.

| Command | Purpose | Example |
|---|---|---|
| `index [--refresh]` | fetch/show the cached LabHub index (release, age, counts) with mirror failover | `climg index --refresh` |
| `search [kw] [--kind K] [--vendor V] [--source labhub\|local\|all] [--status built\|cached\|available] [--all]` | table KEY/KIND/VERSION/SIZE/STATUS/SOURCE + next-command hint; no-kind entries need `--all` | `climg search vios --status available` |
| `show <ref>` | files, checksums, kind, vrnetlab dir, target filename, predicted vs `make version-test` tag, status, template snippet | `climg show vsrx` |
| `images` | `docker image ls` joined with the registry; vrnetlab images detected by author label + `uv run /launch.py` entrypoint; dangling ones flagged (never deleted) | `climg images` |
| `lab <file\|dir>... [--pull --yes]` | parse `.clab.yml/.yaml` (node > group > kinds > defaults), classify every image: BUILT, BUILDABLE-CACHED, PULLABLE, VENDOR-FILE, REGISTRY, MISSING (with a hint); `--pull` builds the missing buildable ones. Exit 1 if anything is MISSING | `climg lab ~/containerlab/labs/templates` |
| `pull <ref> [--yes] [--force] [--tag T]` | download (resume, size+md5/sha1, HTML/quota-page detection), extract, build in a work copy, record; no-op when already built | `climg pull labhub:iosv-159-3-m6 --yes` |
| `add <file> [--kind K] [--version V] [--tag T] [--build] [--yes]` | register a vendor file (kind/version from its real vendor name, confirmed with `make version-test`; original path kept) and optionally build it | `climg add ./c8000v-universalk9_16G_serial.17.16.01a.qcow2 --build --yes` |
| `template <ref\|kind> [-o DIR] [--ui JSON]` | write a 2-node `.clab.yml` (eth1<->eth1, credentials from config, QEMU_MEMORY/QEMU_SMP, header with interfaces, login, boot time, pull hint) and/or merge an entry into a containerlab-app `clab-node-templates.json` | `climg template vsrx -o /tmp/tpl` |
| `init [--workspace P] [--images-dir P] [--vrnetlab P] [--app-container NAME] [--yes]` | detect defaults, show them, confirm (unless `--yes`), write the config, create the dirs. Workspace: flag > running app container (configured name, else `containerlab-app`, else any container whose image contains `containerlab-web`) -> its `WEB_LOCAL_WORKSPACE` (else `~/.clab`) > `~/.clab`. Node Template file: host source of the app's `/root/.clab` mount + `/ui/custom-nodes.json` (no such mount: REST only); without an app `~/.clab/ui/custom-nodes.json` | `climg init --yes` |
| `scan` | image files (`*.qcow2 *.vmdk *.tgz *.tar.gz *.bin *.iol`) in the images dir(s): kind, version, predicted tag, `IMPORTED (not built)` / `BUILT`; feeds `search --source local` and `images` | `climg scan` |
| `build <file\|local:key> [--yes]` | `add` + build; prints the Node Template result | `climg build local:FGT_VM64_KVM-v7.6.2.F-build3462-FORTINET.out.kvm.qcow2 --yes` |
| `nodetpl list\|sync [--dry-run]\|add <ref\|image>\|remove <name> [--writer file\|rest]` | manage the web UI Node Templates climg owns (see below) | `climg nodetpl sync` |
| `doctor` | mirrors (Range probe), GitHub API, docker, /dev/kvm, containerlab, vrnetlab, disk vs floor, memory, config | `climg doctor` |
| `test <ref> [--budget S] [--force]` | one-node lab `climg-<alias>-<stamp>`: wait healthy, SSH login (legacy KEX), show-version regex, logs to `runs/`, always `containerlab destroy --cleanup`; result appended to `results.jsonl` | `climg test iosv-159-3-m6` |

Images dirs are auto-discovered: files in `images_dirs` whose names match a kind show up in
`climg search --source local` with no `add` needed (`add` additionally records sha1 and a custom tag).

## Patches

A kind in `kinds.yaml` may list `patches: [<file>, ...]`; the files live in `patches/` (unified diffs, `-p1` relative to the
vrnetlab checkout root, free-text comment header allowed). Every build applies them with `patch -p1` to the private work
copy only (the checkout is never touched); a missing or non-applying patch fails the build with the patch name and the
`patch` output. The applied names + sha256 are stored in `builds.json` (`patches`) and in the docker image label
`climg.patches` (`name@sha256,...`); `climg show` and `climg images` display them. Currently `cisco_vios` carries
`cisco_vios-0001-pr548-bootstrap-stall.patch` (upstream PR srl-labs/vrnetlab#548 for issue #547: first-boot hang of the
vIOS launcher). Drop the entry from `kinds.yaml` once upstream merges it. `climg pull REF --tag T` builds under a temporary
tag first (e.g. to `climg test T` before replacing the real tag).

## Node Templates (containerlab-app node palette)

After every successful `pull`, `add --build` or `build`, climg upserts one Node Template (`name, kind, image, icon,
baseName, interfacePattern, setDefault:false, env QEMU_MEMORY/QEMU_SMP`; no credentials) into
`<app home>/.clab/ui/custom-nodes.json`. Name = `<vendor>-<model>-<version>`, lower-case except the version, e.g.
`cisco-vios-159-3.m6`, `juniper-vsrx-24.4R1.9`, `huawei-ne40e-lh266`, `fortinet-fortigate-7.6.2`,
`paloalto-pa-vm-11.2.5` (model token from the kind: `ui.model` in `kinds.yaml`, else the kind name after the vendor; Huawei
model from the tag prefix). Icons come from `ui.icon` in `kinds.yaml` (names the UI supports: router, switch, leaf,
spine, super-spine, dcgw, firewall, container, vm, server, client, controller, cloud, pon, rgw, ue). vrnetlab kinds use
`eth{n}` as interface pattern. The browser needs a refresh to see changes.

Ownership: names created by climg are listed in `~/.local/share/climg/nodetpl-owned.json`; climg never modifies or
deletes any other template (a clashing foreign name is skipped with a warning), and always re-reads right before writing
and merges only its own entries, under an flock (`locks/nodetpl.lock`). The first write to a missing file starts from the
two built-in defaults the app serves (SRLinux Latest, Network Multitool) so the UI does not lose them.

Your edits are kept: `nodetpl-owned.json` also stores the last entry climg generated per template (`generated`). When
climg regenerates a managed template (pull/add/build/`nodetpl sync`/`nodetpl add`) it does a field-level three-way merge:
fields you changed in the app (including a deleted or an added field, and `env` as a whole) keep your value, untouched
fields take the newly generated value, and `setDefault` stays yours. The kept fields are named in the output (`updated
(kept your interfacePattern)`) and as `kept: [...]` in the JSON (`changes[]`, and `nodetpl` of pull/add/build). A template
written by an older climg (no stored baseline) counts as edited in every field that differs from what climg generates
now. `nodetpl sync` does not delete a template whose image is gone if you edited it: it is reported in `skipped` as
`edited by user; image missing`. `nodetpl remove <name>` still removes it, and `nodetpl list --json` marks such
templates `customized: true`.

Writers (`nodetpl_writer`, `--writer`):

* `rest` - POST/DELETE per entry on `/api/runtime/ui/custom-nodes` (session cookie from `/auth/endpoints`, self-signed
  TLS accepted). **Default when the app answers** (`auto`): the app itself validates and writes the file with its own
  ownership/permissions, needs no docker access and never replaces the whole list.
* `file` - read-modify-write of the JSON file. Writable by the user: written directly; root-owned (the app creates it
  as root): `docker exec -i <app>` writes a temp file and `mv`s it inside the container. Used by `auto` when the app is
  not reachable and the file path is known.

`nodetpl sync` = one template per built vrnetlab image of a known kind (including images not built by climg) and removal
of owned templates whose image is gone; `--dry-run` only reports.

## Machine interface (contract version 1)

For programs that run climg as a subprocess (`climg --version` prints `climg <version> (contract 1)`; `climg config
--json` reports `contract_version`).

* `--json` on `index search show images scan lab pull add build template init config nodetpl doctor`: stdout is exactly
  one JSON document; warnings and progress go to stderr. Text output without `--json` is unchanged. Failures print
  `{"error": {"code": ..., "message": ...}}`. Codes: `needs_yes not_found invalid_ref disk_floor locked no_config
  build_failed download_failed cancelled` (plus `error` for anything else).
* Exit codes: `0` success or no-op, `1` runtime failure, `2` command-line usage error only, `3` needs `--yes`,
  `4` cancelled (SIGTERM), `130` interrupted from the terminal.
* Argument order: all options first, then `--`, then every positional (for `nodetpl` the action is a positional):
  `climg nodetpl --json --writer file --nodetpl-file P -- add <ref>`, `climg show --json -- <ref>`,
  `climg pull --yes --json --job-file F --no-nodetpl -- <ref>`. (`nodetpl <action> --json -- <ref>` does not parse on
  Python <= 3.12.3.)
* `--job-file PATH` on `pull`, `add --build`, `build`, `nodetpl sync`: atomically rewritten
  `{state, phase, pct, message, log, started, updated}`; `state` is `queued|running|succeeded|failed|cancelled`, `phase`
  `wait|download|extract|build|template|done` (`queued`/`wait` while all build slots are busy). The last write also holds
  `result`, the same object `--json` prints.
* SIGTERM stops the build children, runs `docker rm -f` on containers recorded in a `cidfile` of the work copy, removes
  the work copy, keeps a partial download (resumable), releases the locks, writes `cancelled` and exits 4.
* `--no-nodetpl` (pull/add/build) skips the Node Template write; the result's `nodetpl` names the template that would
  be written. `--nodetpl-file PATH` / `CLIMG_NODETPL_FILE` name the template file for `nodetpl` commands.
* No command reads stdin under `--yes` or `--json`; `init` without `--yes` and without a terminal exits 3.
* The configured lab password never appears in `--json` output, job files or log tails (`template --json` and
  `show --json` carry a placeholder). `template -o` still writes the real credentials into the lab file you asked for.

## Safety

climg never modifies the vrnetlab checkout, never executes anything shipped inside an image, never deletes docker
images (`images` only reports dangling ones), builds only after `--yes`, keeps the free disk above `disk_floor_gb`, and
caps concurrent builds. `add --tag T` builds under `T` through a temporary tag that it removes again.

## Known limits

* Kinds marked `confirmed: false` in `kinds.yaml` are derived from the vrnetlab Makefiles but were never smoke-tested.
* Huawei tags embed the positional LabHub id (`ne40e-lh266`); an image counts as built when any `<model>-*` tag exists.
* Only QEMU and IOL index sections are used; DYNAMIPS has no containerlab path.

## License

MIT, see [LICENSE](LICENSE). `patches/cisco_vios-0001-pr548-bootstrap-stall.patch` is the unmodified diff of
[srl-labs/vrnetlab#548](https://github.com/srl-labs/vrnetlab/pull/548) and stays under vrnetlab's MIT license.
climg does not include or redistribute code from ishare2-cli (GPL-3.0); it only reads the public LabHub index format.
Network OS images are not part of this project: obtain and use them under their vendors' license terms.
