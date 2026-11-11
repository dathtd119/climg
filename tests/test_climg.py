import fcntl
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from climg import lab, nodetpl, registry, setup, template  # noqa: E402
from climg.catalog import load_kinds  # noqa: E402
from climg.config import Config  # noqa: E402
from climg.core import Inventory  # noqa: E402
from climg.index import build_records, mirror_urls  # noqa: E402
from climg.util import ClimgError  # noqa: E402

KINDS = load_kinds()
CREDS = {"username": "admin", "password": "test-only-not-a-real-password"}


def fake_index(entries):
    return {"schema_version": "1.0", "url_properties": {
        "protocol": "https", "hostnames": {"main": "m.example", "drive": "d.example", "secondary": "s.example"},
        "prefixes": {"main": "/0:", "drive": "/0:", "secondary": "/1:"}},
        "QEMU": entries, "IOL": [], "DYNAMIPS": []}


def entry(i, name, files):
    fl = [{"filename": f, "path": f"/addons/qemu/{name}/{f}", "size": s, "file_type": "x",
           "extension": os.path.splitext(f)[1],
           "checksum": {"md5": f"{i:032x}", "sha1": f"{i:x}".ljust(40, "a")}} for f, s in files]
    return {"id": i, "name": name, "type": "qemu", "files": fl,
            "metadata": {"install_path": "/x", "total_size": sum(s for _, s in files)}}


class VendorFilenames(unittest.TestCase):
    CASES = [
        ("c8000v-universalk9_16G_serial.17.16.01a.qcow2", "cisco_c8000v", "17.16.01a", "vrnetlab/cisco_c8000v:17.16.01a"),
        ("csr1000v-universalk9.17.03.08a-serial.qcow2", "cisco_csr1000v", "17.03.08a", "vrnetlab/cisco_csr1000v:17.03.08"),
        ("junos-vsrx3-x86-64-24.4R1.9.qcow2", "juniper_vsrx", "24.4R1.9", "vrnetlab/juniper_vsrx:24.4R1.9"),
        ("vJunos-router-23.2R1.15.qcow2", "juniper_vjunosrouter", "23.2R1.15", "vrnetlab/juniper_vjunos-router:23.2R1.15"),
        ("FGT_VM64_KVM-v7.6.2.F-build3462-FORTINET.out.kvm.qcow2", "fortinet_fortigate", "7.6.2",
         "vrnetlab/vr-fortios:7.6.2"),
        ("PA-VM-KVM-11.2.5.qcow2", "paloalto_panos", "11.2.5", "vrnetlab/paloalto_pa-vm:11.2.5"),
        ("vEOS64-lab-4.32.1F.vmdk", "arista_veos", "4.32.1F", "vrnetlab/arista_veos:4.32.1F"),
        ("sros-vm-24.10.R1.qcow2", "nokia_sros", "24.10.R1", "vrnetlab/nokia_sros:24.10.R1"),
        ("nexus9300v64.10.5.2.F.qcow2", "cisco_n9kv", "9300-10.5.2.F", "vrnetlab/cisco_n9kv:9300-10.5.2.F"),
        ("xrv9k-fullk9-x-24.3.1.qcow2", "cisco_xrv9k", "24.3.1", "vrnetlab/cisco_xrv9k:24.3.1"),
        ("cat9kv-prd-17.12.01.qcow2", "cisco_cat9kv", "17.12.01", "vrnetlab/cisco_cat9kv:17.12.01"),
    ]

    def test_examples(self):
        for fn, kind, ver, tag in self.CASES:
            with self.subTest(fn):
                hit = KINDS.detect_vendor_file(fn)
                self.assertIsNotNone(hit, fn)
                self.assertEqual(hit["kind"], kind)
                self.assertEqual(hit["ver"], ver)
                self.assertEqual(KINDS.get(kind).tag_for(hit["target"]), tag)

    def test_vrnetlab_target_names(self):
        self.assertEqual(KINDS.detect_vendor_file("FGT_VM64_KVM-v7.6.2.F-build3462-FORTINET.out.kvm.qcow2")["target"],
                         "fortios-v7.6.2.qcow2")
        self.assertEqual(KINDS.detect_vendor_file("c8000v-universalk9_16G_serial.17.16.01a.qcow2")["target"],
                         "c8000v-17.16.01a.qcow2")
        self.assertEqual(KINDS.detect_vendor_file("nexus9300v64.10.5.2.F.qcow2")["target"], "n9kv-9300-10.5.2.F.qcow2")

    def test_unknown_falls_back_to_kind_and_version(self):
        self.assertIsNone(KINDS.detect_vendor_file("mystery-disk.qcow2"))
        with tempfile.TemporaryDirectory() as d:
            rec = registry.local_record(KINDS, "mystery-disk.qcow2", os.path.join(d, "x"), kind="vsrx", ver="1.2R3")
        self.assertEqual(rec["kind"], "juniper_vsrx")
        self.assertEqual(rec["target"], "junos-vsrx3-x86-64-1.2R3.qcow2")
        self.assertEqual(rec["tag"], "vrnetlab/juniper_vsrx:1.2R3")

    def test_version_override(self):
        rec = registry.local_record(KINDS, "PA-VM-KVM-11.2.5.qcow2", "/x", ver="11.2.5-h1")
        self.assertEqual(rec["target"], "PA-VM-KVM-11.2.5-h1.qcow2")


class LabhubInference(unittest.TestCase):
    NAMES = [
        ("vsrx30-24.4R1.9", "juniper_vsrx"), ("csr1000v-17-03-08a", "cisco_csr1000v"),
        ("c8000v-17.16.01a", "cisco_c8000v"), ("nxosv9k-9300v-10.5.2.F", "cisco_n9kv"),
        ("xrv9k-fullk9-24.3.1", "cisco_xrv9k"), ("iosv-159-3-m6", "cisco_vios"),
        ("viosl2-adventerprisek9-m.ssa.high_iron_20200929", "cisco_vios"), ("asav-962-001", "cisco_asav"),
        ("cat9kv-17.15.01", "cisco_cat9kv"), ("vjunosrouter-24.2R1-S2.5", "juniper_vjunosrouter"),
        ("vjunosswitch-24.4R1.9", "juniper_vjunosswitch"), ("veos-4.33.1.1F", "arista_veos"),
        ("timos-ng-24.7.R1", "nokia_sros"), ("ne40e", "huawei_vrp"), ("ce12800", "huawei_vrp"),
        ("fortinet-FGT-v7.6.2.F-build3462", "fortinet_fortigate"), ("paloalto-11.2.5", "paloalto_panos"),
    ]

    def test_names(self):
        for name, kind in self.NAMES:
            with self.subTest(name):
                hit = KINDS.infer_labhub(name, entry_id=1)
                self.assertIsNotNone(hit)
                self.assertEqual(hit["kind"], kind)

    def test_iol_section(self):
        hit = KINDS.infer_labhub("L3-ADVENTERPRISEK9-M-15.4-2T.bin", section="IOL")
        self.assertEqual(hit["kind"], "cisco_iol")

    def test_unbuildable_has_no_kind(self):
        for n in ("linux-ubuntu-18.04-server", "fortinet-FMG-v7.6.2.F-build3415", "fortinet-FAZ-6-4-build2002"):
            self.assertIsNone(KINDS.infer_labhub(n, entry_id=1), n)

    def test_huawei_tag_follows_id(self):
        hit = KINDS.infer_labhub("ne40e", entry_id=266)
        self.assertEqual(KINDS.get("huawei_vrp").tag_for(hit["target"]), "vrnetlab/huawei_vrp:ne40e-lh266")

    def test_records_keys_and_duplicates(self):
        idx = fake_index([entry(1, "dup", [("a.qcow2", 5)]), entry(2, "dup", [("b.qcow2", 6)]),
                          entry(3, "vsrx30-24.4R1.9", [("vsrx30-24.4R1.9.tgz", 10)])])
        recs = {r["key"]: r for r in build_records(idx, KINDS)}
        self.assertIn("labhub:vsrx30-24.4R1.9", recs)
        self.assertEqual(sum(1 for k in recs if k.startswith("labhub:dup@")), 2)
        self.assertEqual(recs["labhub:vsrx30-24.4R1.9"]["file_mode"], "archive")

    def test_vendor_folder_model(self):
        def at(i, name, path, section="QEMU"):
            e = entry(i, name, [("x.tgz", 1)])
            e["files"][0]["path"] = path
            return e
        idx = fake_index([
            at(1, "h3cvsr1k-7.1.064-R1362P12", "/addons/qemu/H3C/vSR/h3cvsr1k-7.1.064-R1362P12.tgz"),
            at(2, "h3cvswitch_S9850-7.1.070-R7639P06", "/addons/qemu/H3C/vSW/h3cvswitch_S9850-7.1.070-R7639P06.tgz"),
            at(3, "veos-4.33.1.1F", "/addons/qemu/Arista%20vEOS/veos-4.33.1.1F.tgz"),
            at(4, "asav-9-20-2", "/addons/qemu/asav-9-20-2/virtioa.qcow2"),
            at(5, "mystery-1", "/addons/qemu/mystery-1/virtioa.qcow2"),
            at(6, "vsrx30-24.4R1.9", "/addons/qemu/Juniper%20vSRX3.0/vsrx30-24.4R1.9.tgz"),
        ])
        idx["IOL"] = [at(7, "L3-ADVENTERPRISEK9-M-15.4-2T.bin", "/addons/iol/bin/L3-ADVENTERPRISEK9-M-15.4-2T.bin")]
        idx["DYNAMIPS"] = [at(8, "c7200-[1]-a3jk9s-mz.124-25g.image", "/addons/dynamips/c7200-%5B1%5D-a3jk9s-mz.124-25g.image")]
        r = {x["name"]: x for x in build_records(idx, KINDS)}
        self.assertEqual(len(r), 8)
        vsr, vsw = r["h3cvsr1k-7.1.064-R1362P12"], r["h3cvswitch_S9850-7.1.070-R7639P06"]
        self.assertEqual((vsr["vendor"], vsr["folder"], vsr["model"]), ("H3C", "H3C/vSR", "vSR"))
        self.assertEqual((vsw["vendor"], vsw["folder"], vsw["model"]), ("H3C", "H3C/vSW", "vSW"))
        self.assertEqual((r["veos-4.33.1.1F"]["vendor"], r["veos-4.33.1.1F"]["folder"], r["veos-4.33.1.1F"]["model"]),
                         ("Arista", "Arista vEOS", None))
        self.assertEqual(r["asav-9-20-2"]["vendor"], "Cisco")          # bare image-name folder -> name rule
        self.assertEqual(r["mystery-1"]["vendor"], "Other")
        self.assertEqual(r["vsrx30-24.4R1.9"]["vendor"], "Juniper")    # buildable: from the kind
        self.assertEqual(r["vsrx30-24.4R1.9"]["kind"], "juniper_vsrx")
        iol = r["L3-ADVENTERPRISEK9-M-15.4-2T.bin"]
        self.assertEqual((iol["vendor"], iol["folder"], iol["model"], iol["kind"]), ("Cisco", "iol/bin", None, "cisco_iol"))
        dyn = r["c7200-[1]-a3jk9s-mz.124-25g.image"]
        self.assertEqual((dyn["vendor"], dyn["folder"], dyn["model"], dyn["kind"], dyn["key"]),
                         ("Cisco", "dynamips", None, None, "labhub:c7200-[1]-a3jk9s-mz.124-25g.image"))

    def test_kind_vendor_display(self):
        self.assertEqual(KINDS.vendor_of("paloalto_panos"), "Palo Alto")
        self.assertEqual(KINDS.vendor_of("cisco_vios"), "Cisco")
        self.assertEqual(KINDS.vendor_of(None, "Windows", "win-10"), "Microsoft")
        self.assertEqual(KINDS.vendor_of(None, "kali", "kali-2020"), "Linux")
        self.assertEqual(KINDS.vendor_of(None, None, "zzz"), "Other")

    def test_mirror_urls_order_skips_unlisted(self):
        idx = fake_index([])
        urls = mirror_urls(idx, "/addons/x%20y.tgz", ["drive", "legacy", "main"])
        self.assertEqual(urls, ["https://d.example/0:/addons/x%20y.tgz", "https://m.example/0:/addons/x%20y.tgz"])


class ClabPrecedence(unittest.TestCase):
    DOC = yaml.safe_load("""
name: t
topology:
  defaults: {kind: linux, image: img-default}
  kinds:
    cisco_csr1000v: {image: img-kind}
    linux: {image: img-linux}
  groups:
    g1: {kind: cisco_csr1000v, image: img-group}
    g2: {kind: cisco_csr1000v}
  nodes:
    n_node: {kind: cisco_csr1000v, image: img-node, group: g1}
    n_group: {group: g1}
    n_group_kindimg: {group: g2}
    n_kind: {kind: cisco_csr1000v}
    n_default: {}
    n_defkind: {kind: linux}
""")

    def test_precedence(self):
        got = {n: (k, i) for n, k, i in lab.resolve_nodes(self.DOC)}
        self.assertEqual(got["n_node"], ("cisco_csr1000v", "img-node"))
        self.assertEqual(got["n_group"], ("cisco_csr1000v", "img-group"))
        self.assertEqual(got["n_group_kindimg"], ("cisco_csr1000v", "img-kind"))
        self.assertEqual(got["n_kind"], ("cisco_csr1000v", "img-kind"))
        self.assertEqual(got["n_default"], ("linux", "img-linux"))
        self.assertEqual(got["n_defkind"], ("linux", "img-linux"))

    def test_defaults_image_last(self):
        doc = {"topology": {"defaults": {"image": "d"}, "nodes": {"a": {"kind": "x"}}}}
        self.assertEqual(lab.resolve_nodes(doc), [("a", "x", "d")])

    def test_find_topologies_skips_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.clab.yml"), "w").write("name: a\ntopology: {nodes: {}}\n")
            open(os.path.join(d, "b.txt"), "w").write("x")
            locked = os.path.join(d, "locked")
            os.mkdir(locked)
            os.chmod(locked, 0)
            try:
                found = list(lab.find_topologies(d))
            finally:
                os.chmod(locked, 0o755)
        self.assertEqual([os.path.basename(f) for f in found], ["a.clab.yml"])


def make_inv(images, labhub=None, local=None, tmp="/tmp"):
    cfg = Config({"state_dir": tmp, "images_dirs": [os.path.join(tmp, "images")]}, "x")
    inv = Inventory.__new__(Inventory)
    inv.cfg, inv.kinds = cfg, KINDS
    inv.idx = inv.meta = None
    inv.labhub = labhub or []
    inv.local = local or []
    inv.builds = {}
    inv.images = [{"id": "sha256:" + "0" * 64, "tags": [t], "size": 1, "labels": {}, "entrypoint": [],
                   "vrnetlab": True, "dangling": False, "created": ""} for t in images]
    inv.tags = {t: im for im in inv.images for t in im["tags"]}
    return inv


class StatusJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        idx = fake_index([entry(1, "vsrx30-24.4R1.9", [("vsrx30-24.4R1.9.tgz", 10)]),
                          entry(2, "c8000v-17.16.01a", [("c8000v-17.16.01a.tgz", 20)]),
                          entry(3, "iosv-159-3-m6", [("v.qcow2", 5)]),
                          entry(4, "ne40e", [("virtioa.qcow2", 7)]),
                          entry(266, "ce12800", [("virtioa.qcow2", 7)])])
        self.recs = {r["name"]: r for r in build_records(idx, KINDS)}

    def test_built_cached_available(self):
        inv = make_inv(["vrnetlab/juniper_vsrx:24.4R1.9"], list(self.recs.values()), tmp=self.tmp)
        cache = os.path.join(self.tmp, "downloads")
        inv.cfg.downloads_dir  # property exists
        c = self.recs["c8000v-17.16.01a"]["file"]
        os.makedirs(os.path.join(inv.cfg.downloads_dir, c["checksum"]["md5"]))
        open(os.path.join(inv.cfg.downloads_dir, c["checksum"]["md5"], c["filename"]), "w").close()
        self.assertEqual(inv.status(self.recs["vsrx30-24.4R1.9"])[0], "built")
        self.assertEqual(inv.status(self.recs["c8000v-17.16.01a"])[0], "cached")
        self.assertEqual(inv.status(self.recs["iosv-159-3-m6"])[0], "available")

    def test_extra_cache_is_read(self):
        extra = os.path.join(self.tmp, "extra")
        c = self.recs["c8000v-17.16.01a"]["file"]
        os.makedirs(os.path.join(extra, c["checksum"]["md5"]))
        p = os.path.join(extra, c["checksum"]["md5"], c["filename"])
        open(p, "w").close()
        inv = make_inv([], list(self.recs.values()), tmp=self.tmp)
        inv.cfg.extra_cache_dirs = [extra]
        self.assertEqual(inv.status(self.recs["c8000v-17.16.01a"]), ("cached", p))

    def test_family_match_ignores_positional_id(self):
        inv = make_inv(["vrnetlab/huawei_vrp:ce12800-lh70"], list(self.recs.values()), tmp=self.tmp)
        self.assertEqual(self.recs["ce12800"]["tag"], "vrnetlab/huawei_vrp:ce12800-lh266")
        self.assertEqual(inv.status(self.recs["ce12800"])[0], "built")

    def test_local_record_extra_tag_counts_as_built(self):
        rec = registry.local_record(KINDS, "PA-VM-KVM-11.2.5.qcow2", self.tmp, size=1,
                                    extra={"extra_tags": ["vrnetlab/paloalto_pa-vm:test"]})
        inv = make_inv(["vrnetlab/paloalto_pa-vm:test"], local=[rec], tmp=self.tmp)
        self.assertEqual(inv.status(rec), ("built", "vrnetlab/paloalto_pa-vm:test"))

    def test_local_missing_file(self):
        rec = registry.local_record(KINDS, "PA-VM-KVM-11.2.5.qcow2", "/nonexistent/x.qcow2", size=1)
        inv = make_inv([], local=[rec], tmp=self.tmp)
        self.assertEqual(inv.status(rec)[0], "missing-file")

    def test_resolve_refs(self):
        inv = make_inv(["vrnetlab/juniper_vsrx:24.4R1.9"], list(self.recs.values()), tmp=self.tmp)
        self.assertEqual(inv.resolve("vsrx")["name"], "vsrx30-24.4R1.9")
        self.assertEqual(inv.resolve("#2")["name"], "c8000v-17.16.01a")
        self.assertEqual(inv.resolve("labhub:iosv-159-3-m6")["kind"], "cisco_vios")
        self.assertEqual(inv.resolve("vrnetlab/juniper_vsrx:24.4R1.9")["kind"], "juniper_vsrx")

    def test_classify_states(self):
        loc = registry.local_record(KINDS, "PA-VM-KVM-11.2.5.qcow2", __file__, size=1)
        inv = make_inv(["vrnetlab/juniper_vsrx:24.4R1.9", "ghcr.io/nokia/srlinux:latest"],
                       list(self.recs.values()), local=[loc], tmp=self.tmp)
        c = lambda img: lab.classify(inv, img)["state"]  # noqa: E731
        self.assertEqual(c("vrnetlab/juniper_vsrx:24.4R1.9"), lab.BUILT)
        self.assertEqual(c("ghcr.io/nokia/srlinux:latest"), lab.BUILT)
        self.assertEqual(c("ghcr.io/nokia/srlinux"), lab.BUILT)
        self.assertEqual(c("ghcr.io/nokia/other:1"), lab.REGISTRY)
        self.assertEqual(c("vrnetlab/cisco_vios:159-3.m6"), lab.PULLABLE)
        self.assertEqual(c("vrnetlab/paloalto_pa-vm:11.2.5"), lab.VENDOR)
        m = lab.classify(inv, "vrnetlab/cisco_cat9kv:99.9.9")
        self.assertEqual(m["state"], lab.MISSING)
        self.assertIn("cat9kv-prd-99.9.9.qcow2", m["hint"])
        self.assertIn(os.path.join(self.tmp, "images"), m["hint"])
        self.assertEqual(lab.classify(inv, "vrnetlab/nonexistent_thing:1")["state"], lab.MISSING)
        self.assertEqual(lab.classify(inv, None)["state"], lab.MISSING)


class Templates(unittest.TestCase):
    def test_topology_output(self):
        k = KINDS.get("cisco_csr1000v")
        text, doc = template.render_topology(k, "vrnetlab/cisco_csr1000v:17.03.08", CREDS, "17.03.08a",
                                             "labhub:csr1000v-17-03-08a")
        parsed = yaml.safe_load(text)
        self.assertEqual(parsed, doc)
        kdef = parsed["topology"]["kinds"]["cisco_csr1000v"]
        self.assertEqual(kdef["image"], "vrnetlab/cisco_csr1000v:17.03.08")
        self.assertEqual(kdef["credentials"], CREDS)
        self.assertEqual(kdef["env"], {"QEMU_MEMORY": "4096", "QEMU_SMP": "1"})
        self.assertEqual(len(parsed["topology"]["nodes"]), 2)
        self.assertEqual(parsed["topology"]["links"][0]["endpoints"], ["csr1:eth1", "csr2:eth1"])
        self.assertIn("# Interfaces: eth0 = mgmt (GigabitEthernet1); eth1 -> GigabitEthernet2", text)
        self.assertIn("climg pull labhub:csr1000v-17-03-08a --yes", text)
        self.assertIn("Boot time budget", text)

    def test_fortigate_has_no_qemu_env(self):
        text, doc = template.render_topology(KINDS.get("fortinet_fortigate"), "vrnetlab/vr-fortios:7.6.2", CREDS,
                                             "7.6.2", "x")
        self.assertNotIn("env", doc["topology"]["kinds"]["fortinet_fortigate"])

    def test_xrv9k_env_override(self):
        _, doc = template.render_topology(KINDS.get("cisco_xrv9k"), "t", CREDS, "24.3.1", "x")
        self.assertEqual(doc["topology"]["kinds"]["cisco_xrv9k"]["env"]["QEMU_MEMORY"], "16384")

    def test_ui_merge(self):
        k = KINDS.get("juniper_vsrx")
        e = template.ui_entry(k, "vrnetlab/juniper_vsrx:24.4R1.9", "24.4R1.9")
        for f in ("name", "kind", "image", "icon", "interfacePattern"):
            self.assertIn(f, e)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            json.dump([{"name": "SR Linux", "kind": "nokia_srlinux", "image": "x"}], open(p, "w"))
            self.assertEqual(template.merge_ui(p, [e]), (1, 0))
            self.assertEqual(template.merge_ui(p, [e]), (0, 1))
            data = json.load(open(p))
        self.assertEqual([x["name"] for x in data], ["SR Linux", "Juniper vsrx 24.4R1.9"])


class ConfigTests(unittest.TestCase):
    def test_unknown_key_rejected(self):
        from climg.util import ClimgError
        with self.assertRaises(ClimgError):
            Config({"bogus": 1}, "x")

    def test_credentials_required_only_when_used(self):
        from climg.util import ClimgError
        c = Config({}, "x")
        with self.assertRaises(ClimgError):
            c.need_credentials()
        self.assertEqual(Config({"credentials": CREDS}, "x").need_credentials(), CREDS)

    def test_vrnetlab_label_rule(self):
        from climg import dockerx
        lab_ = {"org.opencontainers.image.authors": "roman@dodin.dev"}
        self.assertTrue(dockerx.is_vrnetlab(lab_, ["uv", "run", "/launch.py"]))
        self.assertFalse(dockerx.is_vrnetlab(lab_, ["/bin/sh"]))
        self.assertFalse(dockerx.is_vrnetlab({}, ["uv", "run", "/launch.py"]))


class NodeTemplateTests(unittest.TestCase):
    TAGS = {
        "vrnetlab/cisco_c8000v:17.16.01a": "cisco-c8000v-17.16.01a",
        "vrnetlab/cisco_csr1000v:17.03.08": "cisco-csr1000v-17.03.08",
        "vrnetlab/cisco_n9kv:9300-10.5.2.F": "cisco-n9kv-9300-10.5.2.F",
        "vrnetlab/cisco_xrv9k:24.3.1": "cisco-xrv9k-24.3.1",
        "vrnetlab/cisco_vios:159-3.m6": "cisco-vios-159-3.m6",
        "vrnetlab/juniper_vsrx:24.4R1.9": "juniper-vsrx-24.4R1.9",
        "vrnetlab/huawei_vrp:ne40e-lh266": "huawei-ne40e-lh266",
        "vrnetlab/huawei_vrp:ce12800-lh70": "huawei-ce12800-lh70",
        "vrnetlab/vr-fortios:7.6.2": "fortinet-fortigate-7.6.2",
        "vrnetlab/paloalto_pa-vm:11.2.5": "paloalto-pa-vm-11.2.5",
    }
    BASES = {"cisco-c8000v-17.16.01a": "c8kv", "cisco-csr1000v-17.03.08": "csr", "cisco-n9kv-9300-10.5.2.F": "n9kv",
             "cisco-xrv9k-24.3.1": "xrv9k", "cisco-vios-159-3.m6": "vios", "juniper-vsrx-24.4R1.9": "vsrx",
             "huawei-ne40e-lh266": "ne40e", "huawei-ce12800-lh70": "ce12800", "fortinet-fortigate-7.6.2": "fgt",
             "paloalto-pa-vm-11.2.5": "pan"}

    def test_name_rule_all_ten(self):
        for tag, name in self.TAGS.items():
            with self.subTest(tag):
                e = nodetpl.entry_for_tag(KINDS, tag)
                self.assertEqual(e["name"], name)
                self.assertEqual(e["baseName"], self.BASES[name])
                self.assertEqual(e["image"], tag)
                self.assertFalse(e["setDefault"])
                self.assertEqual(e["interfacePattern"], self.PATTERNS.get(tag, "eth{n}"))
                self.assertNotIn("credentials", e)

    PATTERNS = {"vrnetlab/juniper_vsrx:24.4R1.9": "ge-0/0/{n:0}", "vrnetlab/cisco_csr1000v:17.03.08": "GigabitEthernet{n:2}",
                "vrnetlab/cisco_c8000v:17.16.01a": "GigabitEthernet{n:2}", "vrnetlab/cisco_n9kv:9300-10.5.2.F": "Ethernet1/{n}",
                "vrnetlab/cisco_xrv9k:24.3.1": "GigabitEthernet0/0/0/{n:0}", "vrnetlab/vr-fortios:7.6.2": "port{n:2}",
                "vrnetlab/paloalto_pa-vm:11.2.5": "Ethernet1/{n}"}

    def test_iol_pattern_skips_mgmt_port(self):
        pat = nodetpl.entry_for_tag(KINDS, "vrnetlab/cisco_iol:17.12.1")["interfacePattern"].split(",")
        self.assertEqual(pat[0], "Ethernet0/{n:1-3}")                       # Ethernet0/0 is mgmt
        self.assertEqual(pat[1:], [f"Ethernet{i}/{{n:0-3}}" for i in range(1, 10)])

    def test_unaliased_kinds_keep_eth(self):                                # no containerlab alias -> eth{n}
        for tag in ("vrnetlab/huawei_vrp:ne40e-lh266", "vrnetlab/cisco_vios:159-3.m6"):
            self.assertEqual(nodetpl.entry_for_tag(KINDS, tag)["interfacePattern"], "eth{n}", tag)

    def test_ui_entry_uses_the_nodetpl_pattern(self):
        for kn, tag, ver in (("juniper_vsrx", "vrnetlab/juniper_vsrx:24.4R1.9", "24.4R1.9"),
                             ("cisco_iol", "vrnetlab/cisco_iol:17.12.1", "17.12.1"),
                             ("huawei_vrp", "vrnetlab/huawei_vrp:ne40e-lh266", "ne40e-lh266")):
            self.assertEqual(template.ui_entry(KINDS.get(kn), tag, ver)["interfacePattern"],
                             nodetpl.entry_for_tag(KINDS, tag)["interfacePattern"], kn)

    def test_icons_and_env(self):
        self.assertEqual(nodetpl.entry_for_tag(KINDS, "vrnetlab/vr-fortios:7.6.2")["icon"], "firewall")
        self.assertEqual(nodetpl.entry_for_tag(KINDS, "vrnetlab/cisco_xrv9k:24.3.1")["env"]["QEMU_MEMORY"], "16384")
        self.assertNotIn("env", nodetpl.entry_for_tag(KINDS, "vrnetlab/cisco_vios:159-3.m6"))
        self.assertIsNone(nodetpl.entry_for_tag(KINDS, "ghcr.io/nokia/srlinux:latest"))

    def test_merge_owned_only(self):
        user = {"name": "ui-test-keep", "kind": "linux", "image": "x"}
        clash = {"name": "cisco-vios-159-3.m6", "kind": "linux", "image": "mine"}
        mine = nodetpl.entry_for_tag(KINDS, "vrnetlab/juniper_vsrx:24.4R1.9")
        cur = [dict(user), dict(clash), dict(mine)]
        e_vios = nodetpl.entry_for_tag(KINDS, "vrnetlab/cisco_vios:159-3.m6")
        new, ch, sk = nodetpl.merge(cur, [("upsert", e_vios), ("upsert", mine), ("delete", "ui-test-keep"),
                                          ("delete", mine["name"])], {mine["name"]})
        self.assertEqual([c["name"] for c in new], ["ui-test-keep", "cisco-vios-159-3.m6"])
        self.assertEqual(new[1], clash)                       # unowned clash untouched
        self.assertEqual(ch, [("deleted", mine["name"], [])])
        self.assertEqual({n for n, _ in sk}, {"cisco-vios-159-3.m6", "ui-test-keep"})
        again, ch2, _ = nodetpl.merge(new, [("upsert", mine)], set())
        self.assertEqual(ch2, [("created", mine["name"], [])])
        _, ch3, _ = nodetpl.merge(again, [("upsert", mine)], {mine["name"]})
        self.assertEqual(ch3, [])                              # idempotent, nothing duplicated

    def test_defaults_preserved_on_first_create(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config({"state_dir": d, "nodetpl_file": os.path.join(d, "ui", "custom-nodes.json"),
                          "nodetpl_url": "https://127.0.0.1:1"}, "x")
            w = nodetpl.FileWriter(cfg)
            e = nodetpl.entry_for_tag(KINDS, "vrnetlab/cisco_vios:159-3.m6")
            ch, _, via = nodetpl.apply_ops(cfg, [("upsert", e)], w)
            data = json.load(open(cfg.nodetpl_file))
            self.assertEqual([c["name"] for c in data], ["SRLinux Latest", "Network Multitool", e["name"]])
            self.assertEqual(sum(1 for c in data if c.get("setDefault")), 1)
            self.assertEqual(nodetpl.load_owned(cfg), {e["name"]})
            self.assertEqual(oct(os.stat(cfg.nodetpl_file).st_mode & 0o777), "0o640")

    def test_sync_removes_owned_with_gone_image(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config({"state_dir": d}, "x")
            nodetpl.save_owned(cfg, {"cisco-vios-159-3.m6", "gone-1"})
            im = [{"vrnetlab": True, "tags": ["vrnetlab/cisco_vios:159-3.m6"]}]
            ops = nodetpl.sync_ops(cfg, KINDS, im)
            self.assertEqual([o[0] for o in ops], ["upsert", "prune"])
            self.assertEqual(ops[1][1], "gone-1")


class KeepUserEditsTests(unittest.TestCase):
    VSRX = "vrnetlab/juniper_vsrx:24.4R1.9"
    XRV = "vrnetlab/cisco_xrv9k:24.3.1"          # has env

    def gen(self, tag=None, **over):
        return {**nodetpl.entry_for_tag(KINDS, tag or self.VSRX), **over}

    def test_old_eth_pattern_migrates_with_and_without_baseline(self):
        new = self.gen()
        old = {**new, "interfacePattern": "eth{n}"}
        for base in (old, None):
            with self.subTest(base=base is not None):
                out, ch, _ = self.up([old], new, base)
                self.assertEqual(out[0]["interfacePattern"], "ge-0/0/{n:0}")
                self.assertEqual(ch, [("updated", new["name"], [])])
                self.assertFalse(nodetpl.is_customized(old, base, nodetpl.regen_for(KINDS)))

    def test_user_pattern_kept_with_and_without_baseline(self):
        new = self.gen()
        old = {**new, "interfacePattern": "eth{n}"}
        mine = {**old, "interfacePattern": "ge-0/0/{n:5}"}
        for base in (old, None):
            with self.subTest(base=base is not None):
                out, ch, _ = self.up([mine], new, base)
                self.assertEqual(out[0]["interfacePattern"], "ge-0/0/{n:5}")
                self.assertEqual(ch, [])
                self.assertTrue(nodetpl.is_customized(mine, base, nodetpl.regen_for(KINDS)))

    def test_no_baseline_entry_equal_to_generated_is_unchanged(self):
        new = self.gen()
        self.assertEqual(self.up([new], new, None)[1], [])

    def up(self, cur, theirs, base, owned=True):
        return nodetpl.merge(cur, [("upsert", theirs)], {theirs["name"]} if owned else set(),
                             {theirs["name"]: base} if base is not None else {}, nodetpl.regen_for(KINDS))

    def test_unchanged_fields_take_new(self):
        base = self.gen()
        new, ch, _ = self.up([dict(base)], self.gen(icon="switch", image="vrnetlab/juniper_vsrx:25.1"), base)
        self.assertEqual(new[0]["icon"], "switch")
        self.assertEqual(ch, [("updated", base["name"], [])])

    def test_user_changed_field_kept_others_updated(self):
        base = self.gen()
        cur = {**base, "interfacePattern": "gei-0/1/{n}"}
        new, ch, _ = self.up([cur], self.gen(icon="switch"), base)
        self.assertEqual(new[0]["interfacePattern"], "gei-0/1/{n}")
        self.assertEqual(new[0]["icon"], "switch")
        self.assertEqual(ch, [("updated", base["name"], ["interfacePattern"])])

    def test_user_deleted_key_stays_deleted(self):
        base = self.gen(self.XRV)
        cur = {k: v for k, v in base.items() if k != "env"}
        new, ch, _ = self.up([cur], self.gen(self.XRV, icon="switch"), base)
        self.assertNotIn("env", new[0])
        self.assertEqual(ch[0][2], ["env"])

    def test_user_added_key_stays(self):
        base = self.gen()
        new, ch, _ = self.up([{**base, "extra": 1}], self.gen(icon="switch"), base)
        self.assertEqual(new[0]["extra"], 1)
        self.assertEqual(ch[0][2], ["extra"])

    def test_nested_env_compared_whole(self):
        base = self.gen(self.XRV)
        cur = {**base, "env": {**base["env"], "QEMU_MEMORY": "1"}}
        theirs = self.gen(self.XRV, env={**base["env"], "OTHER": "x"}, icon="switch")
        new, ch, _ = self.up([cur], theirs, base)
        self.assertEqual(new[0]["env"], cur["env"])
        self.assertEqual(ch[0][2], ["env"])

    def test_setdefault_stays_users(self):
        base = self.gen()
        new, ch, _ = self.up([{**base, "setDefault": True}], self.gen(), base)
        self.assertTrue(new[0]["setDefault"])
        self.assertEqual(ch, [])

    def test_no_base_equal_to_regenerated_is_unchanged(self):
        cur = self.gen()
        new, ch, _ = self.up([dict(cur)], self.gen(), None)
        self.assertEqual((new, ch), ([cur], []))
        new, ch, _ = self.up([{**cur, "icon": "old"}], self.gen(), None)      # differs from regenerated: an edit
        self.assertEqual(new[0]["icon"], "old")

    def test_no_base_differs_keeps_edit_fills_missing(self):
        cur = {k: v for k, v in self.gen(self.XRV).items() if k != "env"}
        cur["interfacePattern"] = "gei-0/1/{n}"
        new, ch, _ = self.up([cur], self.gen(self.XRV, icon="switch"), None)
        self.assertEqual(new[0]["interfacePattern"], "gei-0/1/{n}")
        self.assertIn("env", new[0])                       # missing field filled
        self.assertEqual(new[0]["icon"], "router")         # differs from theirs -> treated as edit, kept
        self.assertIn("interfacePattern", ch[0][2])

    def test_sync_prune_skips_edited(self):
        e = self.gen()
        edited = {**e, "interfacePattern": "gei-0/1/{n}"}
        for base in (e, None):                             # with base / migration (regenerated)
            new, ch, sk = nodetpl.merge([edited], [("prune", e["name"])], {e["name"]},
                                        {e["name"]: base} if base else {}, nodetpl.regen_for(KINDS))
            self.assertEqual((new, ch), ([edited], []))
            self.assertEqual(sk, [(e["name"], "edited by user; image missing")])

    def test_sync_prune_deletes_untouched_and_remove_forces(self):
        e = self.gen()
        edited = {**e, "interfacePattern": "x"}
        _, ch, sk = nodetpl.merge([dict(e)], [("prune", e["name"])], {e["name"]}, {e["name"]: e}, None)
        self.assertEqual((ch, sk), ([("deleted", e["name"], [])], []))
        new, ch, _ = nodetpl.merge([edited], [("delete", e["name"])], {e["name"]}, {e["name"]: e}, None)
        self.assertEqual((new, ch), ([], [("deleted", e["name"], [])]))

    def test_owned_file_formats_and_atomic_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config({"state_dir": d}, "x")
            os.makedirs(os.path.dirname(cfg.nodetpl_owned_file), exist_ok=True)
            with open(cfg.nodetpl_owned_file, "w") as f:
                json.dump({"names": ["a"]}, f)                                    # older format
            self.assertEqual(nodetpl.load_owned(cfg), {"a"})
            self.assertEqual(nodetpl.load_generated(cfg), {})
            nodetpl.save_owned(cfg, {"a"}, {"a": {"name": "a"}})
            with open(cfg.nodetpl_owned_file) as f:
                doc = json.load(f)
            self.assertEqual(doc, {"names": ["a"], "generated": {"a": {"name": "a"}}})

    def test_apply_ops_end_to_end_json_and_list(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config({"state_dir": d, "nodetpl_file": os.path.join(d, "ui", "custom-nodes.json"),
                          "nodetpl_url": "https://127.0.0.1:1"}, "x")
            w = nodetpl.FileWriter(cfg)
            e = self.gen()
            nodetpl.apply_ops(cfg, [("upsert", e)], w, kinds=KINDS)
            with open(cfg.nodetpl_file) as f:
                data = json.load(f)
            for c in data:
                if c["name"] == e["name"]:
                    c["interfacePattern"] = "gei-0/1/{n}"
            with open(cfg.nodetpl_file, "w") as f:
                json.dump(data, f)
            ch, _, _ = nodetpl.apply_ops(cfg, [("upsert", self.gen(icon="switch"))], w, kinds=KINDS)
            self.assertEqual(ch, [("updated", e["name"], ["interfacePattern"])])
            with open(cfg.nodetpl_file) as f:
                cur = {c["name"]: c for c in json.load(f)}[e["name"]]
            self.assertEqual((cur["interfacePattern"], cur["icon"]), ("gei-0/1/{n}", "switch"))
            self.assertEqual(nodetpl.load_generated(cfg)[e["name"]]["icon"], "switch")
            self.assertTrue(nodetpl.is_customized(cur, nodetpl.load_generated(cfg)[e["name"]]))


class ApplyOpsLockTests(unittest.TestCase):
    """nodetpl.lock must be free while a REST commit runs (the app mutex may be held by a file-writer run waiting for it)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.cfg = Config({"state_dir": d, "nodetpl_file": os.path.join(d, "ui", "custom-nodes.json"),
                           "nodetpl_url": "https://127.0.0.1:1"}, "x")
        self.e = nodetpl.entry_for_tag(KINDS, "vrnetlab/cisco_vios:159-3.m6")

    def lock_free(self):
        with open(self.cfg.nodetpl_lock_file, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fh, fcntl.LOCK_UN)
            return True

    def fake_rest(self, during):
        outer = self

        class FakeRest(nodetpl.Rest):
            name = "rest"

            def __init__(self):
                self.store = []

            def read(self):
                return [dict(c) for c in self.store]

            initial = read

            def commit(self, new, changes):
                outer.free_during_commit = outer.lock_free()
                during()
                self.store = [dict(c) for c in new]
        return FakeRest()

    def test_rest_releases_lock_and_records_after_commit(self):
        other = {"name": "from-concurrent-run", "kind": "linux", "image": "x"}

        def concurrent():                     # another climg run completes between our release and re-lock
            with nodetpl.locked(self.cfg):
                nodetpl.save_owned(self.cfg, {other["name"]}, {other["name"]: other})

        w = self.fake_rest(concurrent)
        ch, _, via = nodetpl.apply_ops(self.cfg, [("upsert", self.e)], w, kinds=KINDS)
        self.assertEqual((ch, via), ([("created", self.e["name"], [])], "rest"))
        self.assertTrue(self.free_during_commit)
        self.assertEqual(nodetpl.load_owned(self.cfg), {self.e["name"], other["name"]})
        gen = nodetpl.load_generated(self.cfg)
        self.assertEqual(gen[self.e["name"]], self.e)
        self.assertIn(other["name"], gen)
        self.assertTrue(self.lock_free())

    def test_rest_failure_records_nothing_applied(self):
        w = self.fake_rest(lambda: None)

        def boom(new, changes):
            self.free_during_commit = self.lock_free()
            raise ClimgError("unreachable")
        w.commit = boom
        with self.assertRaises(ClimgError):
            nodetpl.apply_ops(self.cfg, [("upsert", self.e)], w, kinds=KINDS)
        self.assertTrue(self.free_during_commit)
        self.assertEqual(nodetpl.load_owned(self.cfg), set())

    def rest_store(self, names, fail_on=None):
        """FakeRest whose store applies each successful request; `fail_on` ("DELETE"/"POST") raises instead."""
        w = self.fake_rest(lambda: None)
        w.store = [{"name": n, "kind": "linux", "image": "x"} for n in names]

        def commit(new, changes):
            by = {c["name"]: c for c in new}
            w.applied = []
            for ch in changes:
                action, name, _ = ch
                verb = "DELETE" if action == "deleted" else "POST"
                if verb == fail_on:
                    raise ClimgError(f"{verb} {name}: HTTP 500")
                if action == "deleted":
                    w.store = [c for c in w.store if c["name"] != name]
                else:
                    w.store = [c for c in w.store if c["name"] != name] + [dict(by[name])]
                w.applied.append(ch)
        w.commit = commit
        return w

    def own(self, names):
        nodetpl.save_owned(self.cfg, set(names), {n: {"name": n, "kind": "linux", "image": "x"} for n in names})

    def assert_owned_is_store(self, w, names):
        self.assertEqual({c["name"] for c in w.store}, set(names))
        self.assertEqual(nodetpl.load_owned(self.cfg), set(names))

    def test_rest_delete_failure_keeps_ownership(self):
        w = self.rest_store(["A"], fail_on="DELETE")
        self.own(["A"])
        with self.assertRaises(ClimgError):
            nodetpl.apply_ops(self.cfg, [("delete", "A")], w, kinds=KINDS)
        self.assert_owned_is_store(w, ["A"])

    def test_rest_delete_success_disowns(self):
        w = self.rest_store(["A"])
        self.own(["A"])
        nodetpl.apply_ops(self.cfg, [("delete", "A")], w, kinds=KINDS)
        self.assert_owned_is_store(w, [])

    def test_rest_sync_post_failure_keeps_pruned_owned(self):
        w = self.rest_store(["B"], fail_on="POST")
        self.own(["B"])
        ops = [("upsert", {"name": "C", "kind": "linux", "image": "x"}), ("prune", "B")]
        with self.assertRaises(ClimgError):
            nodetpl.apply_ops(self.cfg, ops, w, kinds=KINDS)
        self.assert_owned_is_store(w, ["B"])

    def test_rest_sync_delete_failure_keeps_unpruned_owned(self):
        w = self.rest_store(["B"], fail_on="DELETE")
        self.own(["B"])
        ops = [("upsert", {"name": "C", "kind": "linux", "image": "x"}), ("prune", "B")]
        with self.assertRaises(ClimgError):
            nodetpl.apply_ops(self.cfg, ops, w, kinds=KINDS)
        self.assert_owned_is_store(w, ["B", "C"])

    def test_file_writer_holds_lock_across_commit(self):
        w = nodetpl.FileWriter(self.cfg)
        real, seen = w.commit, []

        def commit(new, changes):
            seen.append(self.lock_free())
            real(new, changes)
        w.commit = commit
        nodetpl.apply_ops(self.cfg, [("upsert", self.e)], w, kinds=KINDS)
        self.assertEqual(seen, [False])
        self.assertEqual(nodetpl.load_owned(self.cfg), {self.e["name"]})
        self.assertTrue(self.lock_free())


class InitTests(unittest.TestCase):
    APP = [("containerlab-app", "ghcr.io/srl-labs/containerlab-web:latest")]

    def det(self, flags=None, containers=None, ws="/home/u/.clab", src="/home/u/.clab", existing=None):
        return setup.detect(flags or {}, existing, containers if containers is not None else self.APP,
                            lambda n: (ws, src))[0]

    def test_workspace_precedence(self):
        self.assertEqual(self.det({"workspace": "/w"})["workspace"], "/w")                       # flag first
        self.assertEqual(self.det(ws="/app/ws")["workspace"], "/app/ws")                         # then app
        self.assertEqual(self.det(ws=None)["workspace"], os.path.expanduser("~/.clab"))          # app without env
        self.assertEqual(self.det(containers=[])["workspace"], os.path.expanduser("~/.clab"))    # no app

    def test_app_discovery_and_template_file(self):
        other = [("x", "ghcr.io/srl-labs/containerlab-web:1.2")]
        self.assertEqual(self.det(containers=other)["app_container"], "x")
        both = other + self.APP
        self.assertEqual(self.det(containers=both)["app_container"], "containerlab-app")
        self.assertEqual(self.det({"app_container": "x"}, both)["app_container"], "x")
        self.assertEqual(self.det()["nodetpl_file"], "/home/u/.clab/ui/custom-nodes.json")
        self.assertIsNone(self.det(src=None)["nodetpl_file"])                                    # REST only
        self.assertEqual(self.det(containers=[])["nodetpl_file"],
                         os.path.join(os.path.expanduser("~/.clab"), "ui", "custom-nodes.json"))

    def test_defaults(self):
        c = self.det()
        self.assertEqual(c["lab_roots"], [c["workspace"]])
        self.assertEqual(c["images_dirs"], [os.path.expanduser("~/.local/share/climg/images")])


class NoConfigTests(unittest.TestCase):
    def test_no_config_error(self):
        import contextlib
        import io
        from climg.cli import main
        for cmd in (["images"], ["scan"], ["nodetpl", "list"], ["lab"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main(["--config", "/nonexistent/climg.yaml"] + cmd)
            self.assertEqual(rc, 1)
            self.assertIn("no config: run `climg init`", err.getvalue())


class ScanTests(unittest.TestCase):
    def test_scan_files(self):
        with tempfile.TemporaryDirectory() as d:
            for fn in ("junos-vsrx3-x86-64-24.4R1.9.qcow2", "notes.txt", "weird.qcow2"):
                open(os.path.join(d, fn), "w").close()
            cfg = Config({"state_dir": d, "images_dirs": [d]}, "x")
            res = {f["filename"]: f["rec"] for f in registry.scan_files(cfg, KINDS)}
        self.assertEqual(set(res), {"junos-vsrx3-x86-64-24.4R1.9.qcow2", "weird.qcow2"})
        self.assertEqual(res["junos-vsrx3-x86-64-24.4R1.9.qcow2"]["tag"], "vrnetlab/juniper_vsrx:24.4R1.9")
        self.assertIsNone(res["weird.qcow2"])


if __name__ == "__main__":
    unittest.main()


class PatchTests(unittest.TestCase):
    def setUp(self):
        from climg import build
        self.build = build
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.work = os.path.join(self.tmp, "work")
        self.pdir = os.path.join(self.tmp, "patches")
        os.makedirs(os.path.join(self.work, "a"))
        os.makedirs(self.pdir)
        with open(os.path.join(self.work, "a", "f.txt"), "w") as f:
            f.write("one\ntwo\n")
        self.good = ("# header comment\n--- a/a/f.txt\n+++ b/a/f.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+three\n")
        self.bad = "--- a/a/f.txt\n+++ b/a/f.txt\n@@ -1,2 +1,2 @@\n zzz\n-two\n+x\n"

    def kind(self, patches):
        d = dict(KINDS.get("cisco_vios").d)
        d["patches"] = patches
        from climg.catalog import Kind
        return Kind("cisco_vios", d)

    def put(self, name, text):
        with open(os.path.join(self.pdir, name), "w") as f:
            f.write(text)

    class _Log:
        fh = __import__("io").StringIO()

        def __call__(self, *a, **k):
            pass

    log = _Log()

    def test_selection_from_kinds_yaml(self):
        self.assertEqual(KINDS.get("cisco_vios").patches, ["cisco_vios-0001-pr548-bootstrap-stall.patch"])
        self.assertEqual(KINDS.get("juniper_vsrx").patches, [])
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "patches", "cisco_vios-0001-pr548-bootstrap-stall.patch")))

    def test_bad_patch_name_rejected(self):
        with self.assertRaises(Exception):
            self.kind(["../evil.patch"])

    def test_apply_ok_records_sha256(self):
        self.put("ok.patch", self.good)
        res = self.build.apply_patches(self.kind(["ok.patch"]), self.work, self.log, self.pdir)
        self.assertEqual(open(os.path.join(self.work, "a", "f.txt")).read(), "one\nthree\n")
        self.assertEqual(res[0]["name"], "ok.patch")
        self.assertEqual(len(res[0]["sha256"]), 64)
        self.assertEqual(self.build.patch_label(res), f"ok.patch@{res[0]['sha256']}")
        self.assertEqual(self.build.image_patches({"labels": {"climg.patches": self.build.patch_label(res)}}), res)
        self.assertEqual(self.build.image_patches({"Config": {"Labels": {}}}), [])

    def test_no_patches_is_noop(self):
        self.assertEqual(self.build.apply_patches(self.kind([]), self.work, self.log, self.pdir), [])

    def test_failure_paths(self):
        from climg.util import ClimgError
        self.put("bad.patch", self.bad)
        with self.assertRaises(ClimgError) as c:
            self.build.apply_patches(self.kind(["bad.patch"]), self.work, self.log, self.pdir)
        self.assertIn("bad.patch", str(c.exception))
        self.assertIn("does not apply", str(c.exception))
        with self.assertRaises(ClimgError) as c:
            self.build.apply_patches(self.kind(["missing.patch"]), self.work, self.log, self.pdir)
        self.assertIn("not found", str(c.exception))
