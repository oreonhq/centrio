# Anaconda/blivet-style root storage via libblockdev (BlockDev 3).

import json
import os
import tempfile
import time

import backend


SCHEME_THIN = "lvm_thin"
SCHEME_LVM = "lvm"
SCHEME_BTRFS = "btrfs"

_LVM_POOL = "pool"
_LVM_ROOT = "root"
_LVM_HOME = "home"


def _dm_leaf(name):
    return str(name).replace("-", "--")


def mapper_path(vg_name, lv_name):
    return f"/dev/mapper/{_dm_leaf(vg_name)}-{_dm_leaf(lv_name)}"


def _mib(n):
    return int(n) * 1024 * 1024


def _wait_path(path, timeout=60):
    deadline = time.time() + max(5, int(timeout))
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.2)
    return False


_BD_SCRIPT = r'''
import json
import os
import subprocess
import sys
import time

import gi
gi.require_version("BlockDev", "3.0")
from gi.repository import BlockDev


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def wait_path(path, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.2)
    return False


def dm_leaf(name):
    return str(name).replace("-", "--")


def mapper(vg, lv):
    return "/dev/mapper/%s-%s" % (dm_leaf(vg), dm_leaf(lv))


def settle(sec=30):
    os.system("udevadm settle --timeout=%d" % int(sec))


def scrub_vg_dm(vg):
    leaf = dm_leaf(vg)
    prefix = leaf + "-"
    os.system("vgchange -an %s 2>/dev/null" % vg)
    for _pass in range(3):
        names = []
        try:
            out = subprocess.check_output(["dmsetup", "ls"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            out = ""
        for line in (out or "").splitlines():
            name = (line.split() or [""])[0].strip()
            if name == leaf or name.startswith(prefix):
                names.append(name)
        if not names:
            break
        def prio(n):
            if "-tpool" in n:
                return 1
            if "_tdata" in n or "_tmeta" in n or "-tdata" in n or "-tmeta" in n:
                return 2
            if "pmspare" in n:
                return 3
            return 0
        names.sort(key=lambda n: (prio(n), -len(n), n))
        for name in names:
            os.system("dmsetup remove -f %s 2>/dev/null" % name)
        settle(5)


def size_k(n):
    return "%dK" % (int(n) // 1024)


def main():
    if len(sys.argv) < 2:
        die("missing config path")
    cfg_path = sys.argv[1]
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as e:
        die("failed to read config %s: %s" % (cfg_path, e))
    scheme = cfg["scheme"]
    part = cfg["root_part"]
    fstype = cfg.get("fstype") or "ext4"
    vg = cfg.get("vg_name")
    separate_home = bool(cfg.get("separate_home"))
    root_virt = int(cfg.get("root_virt_bytes") or 0)
    home_virt = int(cfg.get("home_virt_bytes") or 0)
    teardown_vgs = cfg.get("teardown_vgs") or []

    plugins = BlockDev.plugin_specs_from_names(["lvm", "fs"])
    if not BlockDev.reinit(plugins, True, None):
        die("BlockDev.reinit failed")

    if scheme in ("lvm_thin", "lvm"):
        if not vg:
            die("missing vg_name")
        if not os.path.exists(part):
            die("PV partition missing: %s" % part)

        # Isolate PV only for pv/vgcreate. Do not leave --devices on for thin
        # ops (known LVM breakage with thin pool activation).
        BlockDev.lvm_set_devices_filter([part])
        BlockDev.lvm_set_global_config(
            "devices { use_devicesfile = 0 } "
            "backup { backup = 0 archive = 0 }"
        )

        try:
            BlockDev.fs_wipe(part, True, True)
        except Exception as e:
            if "signature" not in str(e).lower() and "no filesystem" not in str(e).lower():
                print("wipe note: %s" % e, file=sys.stderr)

        settle(15)
        if not BlockDev.lvm_pvcreate(part, 0, 0, None):
            die("pvcreate failed for %s" % part)
        settle(15)
        if not BlockDev.lvm_vgcreate(vg, [part], 0, None):
            die("vgcreate failed for %s" % vg)
        settle(15)

        # Unique VG name is the live-session isolation. Clear devices filter
        # before thin/linear LV work so LVM does not pass --devices into thin.
        BlockDev.lvm_set_devices_filter([])

        vgi = BlockDev.lvm_vginfo(vg)
        if not vgi or int(vgi.free) <= 0:
            die("VG %s has no free space" % vg)
        free = int(vgi.free)

        if scheme == "lvm_thin":
            md = int(BlockDev.lvm_get_thpool_meta_size(free, 0, 100) or 0)
            if md <= 0:
                die("could not compute thin pool metadata size")
            pe = int(getattr(vgi, "extent_size", 0) or (4 * 1024 * 1024))
            md = int(BlockDev.lvm_round_size_to_pe(md, pe, True) or md)
            pool_data = free - (2 * md)
            if pool_data <= (64 * 1024 * 1024):
                die("not enough space for thin pool after metadata reserve")

            os.system("modprobe dm-thin-pool 2>/dev/null")

            for old_vg in teardown_vgs:
                scrub_vg_dm(old_vg)
            scrub_vg_dm(vg)
            settle(10)

            if root_virt <= 0:
                root_virt = pool_data

            # lvcreate(8): one shot pool+thin LV avoids separate tpool activation
            thin_create = {
                "--thinpool": "pool",
                "-V": size_k(root_virt),
                "--zero": "n",
                "--wipesignatures": "n",
            }
            if not BlockDev.lvm_lvcreate(
                vg, "root", pool_data, "thin", None, thin_create
            ):
                print(subprocess.getoutput("dmsetup ls --tree"), file=sys.stderr)
                die("thin pool+root create failed for %s" % vg)
            settle(15)

            pool = BlockDev.lvm_lvinfo(vg, "pool")
            if not pool:
                die("thin pool %s/pool missing after create" % vg)
            segtype = str(getattr(pool, "segtype", "") or "")
            if segtype and segtype != "thin-pool":
                die("LV %s/pool is %s, expected thin-pool" % (vg, segtype))

            root = BlockDev.lvm_lvinfo(vg, "root")
            if not root:
                die("thin root %s/root missing after create" % vg)

            if separate_home:
                if home_virt <= 0:
                    home_virt = max(pool_data // 2, 2 * 1024 * 1024 * 1024)
                thin_extra = [BlockDev.ExtraArg.new("--wipesignatures", "n")]
                if not BlockDev.lvm_thlvcreate(
                    vg, "pool", "home", home_virt, thin_extra
                ):
                    die("thlvcreate failed for %s/home" % vg)
                settle(15)
        else:
            if separate_home:
                root_bytes = max(free * 4 // 10, 8 * 1024 * 1024 * 1024)
                if root_bytes >= free - (2 * 1024 * 1024 * 1024):
                    root_bytes = free // 2
                home_bytes = free - root_bytes
                if not BlockDev.lvm_lvcreate(vg, "root", root_bytes, None, None, None):
                    die("lvcreate root failed")
                settle(15)
                if not BlockDev.lvm_lvcreate(vg, "home", home_bytes, None, None, None):
                    die("lvcreate home failed")
                settle(15)
            else:
                if not BlockDev.lvm_lvcreate(vg, "root", free, None, None, None):
                    die("lvcreate root failed")
                settle(15)

        root_path = mapper(vg, "root")
        if not BlockDev.lvm_lvactivate(vg, "root", False, False, None):
            # may already be active
            pass
        settle(15)
        if not wait_path(root_path, 90):
            die("root LV device never appeared: %s" % root_path)

        targets = [(root_path, "root")]
        if separate_home:
            home_path = mapper(vg, "home")
            BlockDev.lvm_lvactivate(vg, "home", False, False, None)
            settle(15)
            if not wait_path(home_path, 90):
                die("home LV device never appeared: %s" % home_path)
            targets.append((home_path, "home"))

        for path, label in targets:
            if fstype == "xfs":
                if not BlockDev.fs_xfs_mkfs(path, None):
                    die("mkfs.xfs failed on %s" % path)
            elif fstype == "btrfs":
                if not BlockDev.fs_btrfs_mkfs(path, None):
                    die("mkfs.btrfs failed on %s" % path)
            else:
                if not BlockDev.fs_ext4_mkfs(path, None):
                    die("mkfs.ext4 failed on %s" % path)
            settle(10)

        print(json.dumps({
            "root": root_path,
            "home": mapper(vg, "home") if separate_home else None,
            "vg": vg,
            "scheme": scheme,
        }))
        return

    if scheme == "btrfs":
        if not os.path.exists(part):
            die("btrfs partition missing: %s" % part)
        try:
            BlockDev.fs_wipe(part, True, True)
        except Exception:
            pass
        settle(10)
        if not BlockDev.fs_btrfs_mkfs(part, None):
            die("mkfs.btrfs failed on %s" % part)
        settle(10)
        print(json.dumps({"root": part, "home": None, "vg": None, "scheme": scheme}))
        return

    die("unknown scheme: %s" % scheme)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        die("%s: %s" % (type(e).__name__, e))
'''


def apply_root_storage(disk_config, progress_callback=None):
    if not isinstance(disk_config, dict):
        return False, "invalid disk config"

    scheme = disk_config.get("storage_scheme") or SCHEME_THIN
    root_part = disk_config.get("lvm_pv")
    if not root_part:
        return False, "missing root/PV partition"
    fstype = (disk_config.get("filesystem") or "ext4").lower()
    separate_home = bool(disk_config.get("separate_home")) and scheme != SCHEME_BTRFS
    vg_name = disk_config.get("lvm_vg")
    usable_mib = int(disk_config.get("lvm_usable_mib") or 8192)

    if scheme in (SCHEME_THIN, SCHEME_LVM) and not vg_name:
        return False, "missing LVM VG name"

    if scheme == SCHEME_THIN:
        root_virt = _mib(max(8192, usable_mib - 256))
        home_virt = 0
        if separate_home:
            root_virt = _mib(max(40960, int((usable_mib - 256) * 0.4)))
            home_virt = _mib(max(2048, usable_mib - 256)) - root_virt
            if home_virt < _mib(2048):
                home_virt = _mib(2048)
    else:
        root_virt = 0
        home_virt = 0

    cfg = {
        "scheme": scheme,
        "root_part": root_part,
        "fstype": fstype,
        "vg_name": vg_name,
        "separate_home": separate_home,
        "root_virt_bytes": int(root_virt),
        "home_virt_bytes": int(home_virt),
        "teardown_vgs": list(disk_config.get("lvm_teardown_vgs") or []),
    }

    if progress_callback:
        progress_callback(f"Creating {scheme} root storage with libblockdev...", None)

    primary_disk = (disk_config.get("target_disks") or [None])[0]
    scrub_vgs = set(cfg["teardown_vgs"])
    if vg_name:
        scrub_vgs.add(vg_name)
    if primary_disk and scheme in (SCHEME_THIN, SCHEME_LVM):
        ok_td, err_td, disk_vgs = backend.teardown_lvm_on_disk(
            primary_disk, progress_callback
        )
        scrub_vgs.update(disk_vgs or [])
        if not ok_td:
            return False, err_td or f"could not tear down LVM on {primary_disk}"

    if scrub_vgs and scheme == SCHEME_THIN:
        backend.scrub_vg_dm_stacks(sorted(scrub_vgs), progress_callback)

    if scheme in (SCHEME_THIN, SCHEME_LVM) and vg_name:
        backend.purge_stale_vg_dm(vg_name, progress_callback)

    cfg["teardown_vgs"] = sorted(scrub_vgs)

    fd, script_path = tempfile.mkstemp(prefix="centrio_bd_", suffix=".py")
    cfg_path = None
    try:
        os.write(fd, _BD_SCRIPT.encode("utf-8"))
        os.close(fd)
        os.chmod(script_path, 0o700)
        cfd, cfg_path = tempfile.mkstemp(prefix="centrio_bd_cfg_", suffix=".json")
        os.write(cfd, json.dumps(cfg).encode("utf-8"))
        os.close(cfd)
        os.chmod(cfg_path, 0o600)

        ok, err, out = backend._run_command(
            ["python3", script_path, cfg_path],
            f"libblockdev {scheme} layout",
            progress_callback,
            timeout=600,
        )
        if not ok:
            return False, err or "libblockdev storage setup failed"

        result = {}
        for line in reversed((out or "").strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                result = json.loads(line)
                break
            except Exception:
                continue

        root_dev = result.get("root")
        home_dev = result.get("home")
        if scheme in (SCHEME_THIN, SCHEME_LVM):
            root_dev = root_dev or mapper_path(vg_name, _LVM_ROOT)
            if separate_home:
                home_dev = home_dev or mapper_path(vg_name, _LVM_HOME)
            if not _wait_path(root_dev, 30):
                return False, f"root device missing after layout: {root_dev}"
            if separate_home and not _wait_path(home_dev, 30):
                return False, f"home device missing after layout: {home_dev}"
            disk_config["lvm_pool"] = _LVM_POOL if scheme == SCHEME_THIN else None
            disk_config["lvm_root_lv"] = _LVM_ROOT
            disk_config["lvm_home_lv"] = _LVM_HOME if separate_home else None
        else:
            root_dev = root_dev or root_part
            home_dev = None
            disk_config["lvm_pool"] = None
            disk_config["lvm_root_lv"] = None
            disk_config["lvm_home_lv"] = None

        for part in disk_config.get("partitions") or []:
            mp = part.get("mountpoint")
            if mp == "/":
                part["device"] = root_dev
                part["fstype"] = "btrfs" if scheme == SCHEME_BTRFS else fstype
            elif mp == "/home" and separate_home and home_dev:
                part["device"] = home_dev

        if scheme == SCHEME_BTRFS:
            disk_config["btrfs_subvolumes"] = True
            ok_sv, err_sv = backend.create_btrfs_subvolumes(root_dev, progress_callback)
            if not ok_sv:
                return False, err_sv or "btrfs subvolume setup failed"

        return True, ""
    finally:
        for p in (script_path, cfg_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
