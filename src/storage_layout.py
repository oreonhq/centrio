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
    try:
        subprocess.run(["udevadm", "settle", "--timeout=%d" % int(sec)], check=False)
    except OSError:
        pass


def _scrub_vg_dm(vg):
    """Remove only stale mappings for the fresh target VG before LVM creates it."""
    leaf = dm_leaf(vg)
    prefix = leaf + "-"
    subprocess.run(["lvm", "vgchange", "--config", "devices { use_devicesfile = 0 } "
                    "global { event_activation = 0 use_lvmlockd = 0 }",
                    "-an", vg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(3):
        try:
            output = subprocess.check_output(["dmsetup", "ls"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            output = ""
        names = [line.split()[0] for line in output.splitlines() if line.split() and (line.split()[0] == leaf or line.split()[0].startswith(prefix))]
        if not names:
            return
        # A thin-pool stack must be removed from consumers to providers:
        # pool/root first, then pool-tpool, then its data/metadata devices.
        # Removing tpool first leaves it held by pool and poisons the retry.
        def remove_priority(name):
            if "-tpool" in name:
                return 1
            if any(token in name for token in ("_tdata", "_tmeta", "-tdata", "-tmeta")):
                return 2
            if "pmspare" in name:
                return 3
            return 0
        for name in sorted(names, key=lambda n: (remove_priority(n), -len(n), n)):
            subprocess.run(["dmsetup", "remove", "--force", "--retry", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        settle(5)


_LVM_CFG = (
    "devices { use_devicesfile = 0 } "
    "global { event_activation = 0 use_lvmlockd = 0 use_lvmpolld = 0 } "
    "activation { auto_activation_volume_list = [] }"
)


def clear_failed_target_layout(vg, part):
    """Remove a prior failed layout only when this PV owns its VG.

    The installer retains its generated VG name on Retry.  A failed thin-pool
    leaves hidden pool/tpool dm mappings behind; pvcreate --force does not
    remove them, so the next lvcreate sees its own stale tpool as in-use.
    """
    probe = subprocess.run(
        ["lvm", "pvs", "--config", _LVM_CFG,
         "--noheadings", "-o", "vg_name", part],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    # A failed thin-pool attempt can erase the PV/VG label while leaving its
    # internal pool-tpool mapping in device-mapper.  The generated VG name is
    # stable for this target disk, so always remove only that prefix first.
    # Waiting for pvs to report an owner here made retries preserve the exact
    # stale mapping that LVM subsequently rejects as "used by another device".
    _scrub_vg_dm(vg)
    owner = (probe.stdout or "").strip()
    if owner != vg:
        settle(10)
        return
    subprocess.run(["lvm", "vgchange", "--config", _LVM_CFG,
                    "--activate", "n", vg], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _scrub_vg_dm(vg)
    subprocess.run(["lvm", "lvremove", "--config", _LVM_CFG,
                    "--force", "--yes", vg], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["lvm", "vgremove", "--config", _LVM_CFG,
                    "--force", "--yes", vg], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["lvm", "pvremove", "--config", _LVM_CFG,
                    "--force", "--yes", part], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _scrub_vg_dm(vg)
    settle(10)


def _sys_block(dev):
    return os.path.basename(os.path.realpath(dev))


def _parent_disk(part):
    name = _sys_block(part)
    sysp = "/sys/class/block/%s" % name
    if os.path.exists(os.path.join(sysp, "partition")):
        return "/dev/" + os.path.basename(os.path.realpath(os.path.join(sysp, "..")))
    return part


def _holder_names(dev):
    names = []
    seen = set()

    def walk(sys_name):
        if not sys_name or sys_name in seen:
            return
        seen.add(sys_name)
        hdir = "/sys/class/block/%s/holders" % sys_name
        if not os.path.isdir(hdir):
            return
        for h in os.listdir(hdir):
            walk(h)
            dmp = "/sys/class/block/%s/dm/name" % h
            try:
                with open(dmp, encoding="utf-8") as fh:
                    n = fh.read().strip()
            except OSError:
                n = h
            if n:
                names.append(n)
    walk(_sys_block(dev))
    return names


def _umount_src(src):
    with open("/proc/mounts", encoding="utf-8") as fh:
        mounts = [ln.split() for ln in fh.read().splitlines()]
    for m in sorted(mounts, key=lambda x: -x[1].count("/") if len(x) > 1 else 0):
        if len(m) < 2:
            continue
        if m[0] == src or m[0].startswith(src + "/") or os.path.realpath(m[0]) == os.path.realpath(src):
            subprocess.run(["umount", "-l", m[1].replace("\\040", " ")], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def release_pv(part):
    disk = _parent_disk(part)
    _umount_src(part)
    subprocess.run(["fuser", "-km", part], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe = subprocess.run(
        ["lvm", "pvs", "--config", _LVM_CFG, "--noheadings", "-o", "pv_name,vg_name"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    owners = set()
    for line in (probe.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        pv, vg = fields[0], fields[1]
        try:
            same = os.path.realpath(pv) == os.path.realpath(part)
            on_disk = os.path.realpath(pv).startswith(os.path.realpath(disk))
        except OSError:
            same = pv == part
            on_disk = pv.startswith(disk)
        if vg and (same or on_disk):
            owners.add(vg)
    for old in sorted(owners):
        subprocess.run(["lvm", "vgchange", "--config", _LVM_CFG, "-an", old], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _scrub_vg_dm(old)
        subprocess.run(["lvm", "vgremove", "--config", _LVM_CFG, "-ff", "-y", old], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _scrub_vg_dm(old)
    for _ in range(12):
        names = _holder_names(part) + _holder_names(disk)
        if not names:
            break
        for name in names:
            mapper = "/dev/mapper/%s" % name
            _umount_src(mapper)
            subprocess.run(["fuser", "-k", mapper], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["dmsetup", "remove", "--force", "--retry", name], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        settle(1)
    subprocess.run(["lvm", "pvremove", "--config", _LVM_CFG, "-ff", "-y", part], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["wipefs", "-a", "-f", part], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    settle(5)


def clear_inactive_pool_stack(vg):
    """Remove an inactive new pool's dm stack in dependency order."""
    leaf = dm_leaf(vg)
    names = [leaf + "-pool", leaf + "-pool-tpool", leaf + "-pool_tdata", leaf + "-pool_tmeta"]
    for name in names:
        subprocess.run(["dmsetup", "remove", "--force", name], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _size_k(value):
    return "%dK" % (int(value) // 1024)


def lvm(args, description):
    """Use the upstream lvm(8) thin-provisioning commands directly."""
    args = list(args)
    if args and args[0] == "lvcreate":
        extra = ["--config", _LVM_CFG]
        pool_arg = ""
        if "--thinpool" in args:
            i = args.index("--thinpool")
            if i + 1 < len(args):
                pool_arg = str(args[i + 1])
        new_pool = "--thinpool" in args and "/" not in pool_arg
        linear = "--thinpool" not in args
        if new_pool or linear:
            extra.extend(["--zero", "n", "--wipesignatures", "n"])
    else:
        extra = ["--config", _LVM_CFG]
    cmd = ["lvm", args[0]] + extra + args[1:]
    proc = subprocess.run(cmd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        die("%s failed: %s" % (description, (proc.stderr or proc.stdout).strip()))
    subprocess.run(["dmsetup", "mknodes"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.stdout


def dump_lvm_state(vg):
    for args in (
        ["vgs", "-o", "+vg_attr,system_id", vg],
        ["pvs", "-a", "-o", "+vg_name"],
        ["lvs", "-a", "-o", "+devices,lv_active,lv_skip_activation", vg],
    ):
        proc = subprocess.run(
            ["lvm", args[0], "--config", _LVM_CFG] + args[1:],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        print("DIAG $ lvm %s\n%s" % (" ".join(args), (proc.stdout or "").strip()))


def activate_lv(vg, lv):
    full = "%s/%s" % (vg, lv)
    cfg = ["--config", _LVM_CFG]
    proc = subprocess.run(
        ["lvm", "lvchange", "-K", "-ay", "--nolocking"] + cfg + [full],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode:
        verbose = subprocess.run(
            ["lvm", "lvchange", "-K", "-ay", "--nolocking", "-vvvv"] + cfg + [full],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        print("VERBOSE lvchange %s:\n%s" % (full, (verbose.stdout or "")[-6000:]))
        if verbose.returncode == 0:
            return
        dump_lvm_state(vg)
        die("activate %s failed: %s" % (full, (proc.stderr or proc.stdout).strip()))
    subprocess.run(["dmsetup", "mknodes"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    settle(10)


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

    proc = subprocess.run(
        ["lvmconfig", "activation/volume_list",
         "activation/auto_activation_volume_list",
         "activation/read_only_volume_list",
         "activation/udev_sync", "global/system_id_source",
         "global/use_lvmlockd", "global/wait_for_locks",
         "devices/use_devicesfile"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    print("LVM CONFIG:\n%s" % (proc.stdout or "").strip())

    # This path owns LVM through lvm(8) below.  Do not load BlockDev's LVM
    # plugin: it can probe/activate the new thin-pool stack concurrently.
    plugins = BlockDev.plugin_specs_from_names(["fs"])
    if not BlockDev.reinit(plugins, True, None):
        die("BlockDev.reinit failed")

    if scheme in ("lvm_thin", "lvm"):
        if not vg:
            die("missing vg_name")
        if not os.path.exists(part):
            die("PV partition missing: %s" % part)

        release_pv(part)
        clear_failed_target_layout(vg, part)
        subprocess.run(["wipefs", "-a", "-f", part], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lvm(["pvcreate", "--yes", "--force", "--force", part], "pvcreate")
        settle(15)
        lvm(["vgcreate", "--yes", vg, part], "vgcreate")
        settle(15)
        free_out = lvm(["vgs", "--noheadings", "--units", "b", "--nosuffix", "-o", "vg_free", vg], "query VG")
        try:
            free = int(float(free_out.split()[0]))
        except (IndexError, ValueError):
            free = 0
        if free <= 0:
            die("VG %s has no free space" % vg)

        if scheme == "lvm_thin":
            if free <= (8 * 1024 * 1024 * 1024):
                die("not enough space for thin pool after metadata reserve")
            if root_virt <= 0:
                root_virt = free
            clear_inactive_pool_stack(vg)
            lvm(["lvcreate", "--yes", "--monitor", "n", "--poolmetadataspare", "n",
                 "--extents", "100%FREE",
                 "--thinpool", "pool",
                 "--virtualsize", _size_k(root_virt),
                 "--name", "root", vg],
                "create thin pool and root")
            settle(10)
            if separate_home:
                if home_virt <= 0:
                    home_virt = max(free // 2, 2 * 1024 * 1024 * 1024)
                lvm(["lvcreate", "--yes", "--thinpool", "%s/pool" % vg,
                     "--virtualsize", _size_k(home_virt), "--name", "home", vg],
                    "create thin home")
                settle(10)
        else:
            if separate_home:
                root_bytes = max(free * 4 // 10, 8 * 1024 * 1024 * 1024)
                if root_bytes >= free - (2 * 1024 * 1024 * 1024):
                    root_bytes = free // 2
                home_bytes = free - root_bytes
                lvm(["lvcreate", "--yes", "--name", "root", "--size", _size_k(root_bytes), vg], "create root LV")
                settle(15)
                lvm(["lvcreate", "--yes", "--name", "home", "--size", _size_k(home_bytes), vg], "create home LV")
                settle(15)
            else:
                lvm(["lvcreate", "--yes", "--name", "root", "--extents", "100%FREE", vg], "create root LV")
                settle(15)

        root_path = mapper(vg, "root")
        activate_lv(vg, "root")
        if not wait_path(root_path, 90):
            dump_lvm_state(vg)
            die("root LV device never appeared: %s" % root_path)

        targets = [(root_path, "root")]
        if separate_home:
            home_path = mapper(vg, "home")
            activate_lv(vg, "home")
            if not wait_path(home_path, 90):
                dump_lvm_state(vg)
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

    scheme = disk_config.get("storage_scheme") or SCHEME_LVM
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
        # Keep virtual allocations below the physical pool.  Thin volumes can
        # overcommit by design, but a fresh install has no reason to do so and
        # LVM warns about it during creation.
        virtual_budget_mib = max(8192, usable_mib * 80 // 100)
        root_virt = _mib(virtual_budget_mib)
        home_virt = 0
        if separate_home:
            root_mib = min(virtual_budget_mib - 2048, max(40960, virtual_budget_mib // 2))
            root_virt = _mib(root_mib)
            home_virt = _mib(virtual_budget_mib - root_mib)
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
    }

    if progress_callback:
        progress_callback(f"Creating {scheme} root storage with LVM CLI...", None)

    primary_disk = (disk_config.get("target_disks") or [None])[0]
    if primary_disk and scheme in (SCHEME_THIN, SCHEME_LVM):
        ok_td, err_td, _ = backend.teardown_lvm_on_disk(
            primary_disk, progress_callback
        )
        if not ok_td:
            return False, err_td or f"could not tear down LVM on {primary_disk}"
        backend.release_block_device(root_part, progress_callback)

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
            f"LVM CLI {scheme} layout",
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
