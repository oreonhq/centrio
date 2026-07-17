# Centrio Installer
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# centrio_installer/install_logic.py
# Backend for bootloader installation (UEFI and BIOS).
#
# UEFI flow (Anaconda / Oreon layout):
# - Use EFI/<vendor> (e.g. almalinux) to match Anaconda-installed systems. Copy signed
#   shim and grub from host /boot/efi/EFI/<vendor>/ to target ESP. Write a stub grub.cfg
#   on the ESP that does search.fs_uuid <root_uuid> root; set prefix=($root)/boot/grub2;
#   configfile $prefix/grub.cfg so the real config lives in /boot/grub2 on the root fs.
# - No grub2-install (use distro signed binaries). NVRAM entry points to shim in vendor dir.

import os
import re
import shutil
import subprocess
import shlex
import tempfile

from utils import get_host_architecture

# Helpers from backend (imported at use site to avoid circular deps)
def _run_command(command_list, description, progress_callback=None, timeout=None, pipe_input=None):
    """Delegate to backend._run_command."""
    from backend import _run_command as _rc
    return _rc(command_list, description, progress_callback, timeout, pipe_input)

def _run_in_chroot(target_root, command_list, description, progress_callback=None, timeout=None, pipe_input=None):
    """Delegate to backend._run_in_chroot."""
    from backend import _run_in_chroot as _rch
    return _rch(target_root, command_list, description, progress_callback, timeout, pipe_input)

def _ensure_directory(path, progress_callback=None):
    """Delegate to backend.ensure_directory."""
    from backend import ensure_directory
    return ensure_directory(path, progress_callback)

def _write_file_as_root(path, content, progress_callback=None):
    """Delegate to backend.write_file_as_root."""
    from backend import write_file_as_root
    return write_file_as_root(path, content, progress_callback)


BOOTLOADER_ID = "Oreon"

# OEM laptop / prebuilt vendors that need firmware-builtin certs enrolled
_SBCTL_OEM_VENDOR_KEYWORDS = (
    "framework",
    "lenovo",
    "dell",
    "hp",
    "apple",
    "panasonic",
    "toshiba",
)

_SETUPMODE_EFIVAR_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"

# --- UEFI and BIOS detection ---
def is_uefi_system():
    return os.path.exists("/sys/firmware/efi")


def _read_sys_vendor():
    path = "/sys/class/dmi/id/sys_vendor"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return (f.read() or "").strip().lower()
    except OSError:
        return ""


def _sbctl_binary(target_root=None):
    """Return sbctl path on host, or under target_root if only there."""
    for candidate in ("/usr/bin/sbctl", "/usr/sbin/sbctl", "/bin/sbctl"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    if target_root:
        for rel in ("usr/bin/sbctl", "usr/sbin/sbctl", "bin/sbctl"):
            p = os.path.join(target_root, rel)
            if os.path.isfile(p):
                return p
    which = shutil.which("sbctl")
    return which


def _is_secure_boot_setup_mode(progress_callback=None):
    """True when firmware is in Setup Mode (keys unlocked for enrollment)."""
    if not is_uefi_system():
        return False

    sbctl = _sbctl_binary()
    if sbctl:
        ok, _, out = _run_command(
            [sbctl, "status"],
            "Check Secure Boot Setup Mode via sbctl",
            progress_callback,
            timeout=30,
        )
        if ok and out:
            for line in out.splitlines():
                low = line.lower().strip()
                if "setup mode" not in low:
                    continue
                # sbctl prints "Setup Mode: <mark> Enabled|Disabled"
                if "enabled" in low:
                    return True
                if "disabled" in low:
                    return False

    # Fallback: EFI SetupMode variable (attr 4 bytes + value 1 byte)
    efivars = "/sys/firmware/efi/efivars"
    try:
        names = os.listdir(efivars)
    except OSError:
        names = []
    for name in names:
        if name.startswith("SetupMode-") and name.endswith(_SETUPMODE_EFIVAR_GUID):
            try:
                with open(os.path.join(efivars, name), "rb") as f:
                    data = f.read()
                if len(data) >= 5:
                    return data[4] == 1
            except OSError:
                pass
            break
    return False


def _ensure_oreon_bootx64(target_root, efi_install_id, progress_callback=None):
    """Make sure /boot/efi/EFI/oreon/bootx64.efi exists on the target ESP."""
    arch = get_host_architecture()
    oreon_dir = os.path.join(target_root, "boot", "efi", "EFI", "oreon")
    dest = os.path.join(oreon_dir, "bootx64.efi")
    if os.path.isfile(dest):
        return dest

    vendor = efi_install_id or BOOTLOADER_ID
    candidates = [
        os.path.join(target_root, "boot", "efi", "EFI", "oreon", "BOOTX64.EFI"),
        os.path.join(target_root, "boot", "efi", "EFI", "oreon", arch["efi_boot"]),
        os.path.join(target_root, "boot", "efi", "EFI", "oreon", arch["efi_shim"]),
        os.path.join(target_root, "boot", "efi", "EFI", vendor, arch["efi_shim"]),
        os.path.join(target_root, "boot", "efi", "EFI", vendor, arch["efi_boot"]),
        os.path.join(target_root, "boot", "efi", "EFI", vendor, "bootx64.efi"),
        os.path.join(target_root, "boot", "efi", "EFI", "BOOT", arch["efi_boot"]),
        os.path.join(target_root, "boot", "efi", "EFI", "BOOT", "bootx64.efi"),
    ]
    src = next((p for p in candidates if os.path.isfile(p)), None)
    if not src:
        return None
    if not _ensure_directory(oreon_dir, progress_callback):
        return None
    ok, err, _ = _run_command(
        ["cp", "-f", src, dest],
        "Stage oreon/bootx64.efi for sbctl signing",
        progress_callback,
        timeout=30,
    )
    if not ok:
        print(f"Warning: could not stage oreon/bootx64.efi: {err}")
        return None
    return dest


def _looks_like_pe(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def _collect_sbctl_sign_targets(target_root, efi_install_id, progress_callback=None):
    paths = []
    bootx64 = _ensure_oreon_bootx64(target_root, efi_install_id, progress_callback)
    if bootx64:
        paths.append("/boot/efi/EFI/oreon/bootx64.efi")
    else:
        print("Warning: /boot/efi/EFI/oreon/bootx64.efi missing, skipping EFI sign target")

    efi_roots = [
        os.path.join(target_root, "boot", "efi", "EFI", "oreon"),
        os.path.join(target_root, "boot", "efi", "EFI", "BOOT"),
    ]
    for efi_dir in efi_roots:
        try:
            for name in sorted(os.listdir(efi_dir)):
                if not name.lower().endswith(".efi"):
                    continue
                abs_p = os.path.join(efi_dir, name)
                if not os.path.isfile(abs_p):
                    continue
                rel = "/" + os.path.relpath(abs_p, target_root)
                paths.append(rel.replace("\\", "/"))
        except OSError:
            pass

    boot_dir = os.path.join(target_root, "boot")
    try:
        names = os.listdir(boot_dir)
    except OSError:
        names = []

    for name in sorted(names):
        if name.startswith("vmlinuz-") and "rescue" not in name:
            paths.append("/boot/" + name)

    out = []
    seen = set()
    for p in paths:
        if p in seen:
            continue
        abs_p = os.path.join(target_root, p.lstrip("/"))
        if not os.path.isfile(abs_p):
            continue
        if not _looks_like_pe(abs_p):
            print(f"Skip non-PE sign target: {p}")
            continue
        seen.add(p)
        out.append(p)
    return out


def provision_secure_boot_with_sbctl(target_root, efi_partition_device=None, efi_install_id=None, progress_callback=None):
    if not is_uefi_system():
        print("Skipping sbctl Secure Boot provisioning (not UEFI).")
        return True, ""

    if progress_callback:
        progress_callback("Checking Secure Boot Setup Mode...", None)

    if not _is_secure_boot_setup_mode(progress_callback):
        print(
            "Secure Boot is not in Setup Mode (keys already locked/populated). "
            "Bypassing sbctl key enrollment for OEM/pre-seeded firmware compatibility."
        )
        return True, ""

    sbctl_host = _sbctl_binary()
    sbctl_target = _sbctl_binary(target_root)
    if not sbctl_host and not sbctl_target:
        print("Warning: sbctl not found on host or target. Skipping Secure Boot provisioning.")
        return True, ""

    use_chroot = bool(sbctl_target)
    if not use_chroot and sbctl_host:
        dest_bin = os.path.join(target_root, "usr", "bin", "sbctl")
        if _ensure_directory(os.path.dirname(dest_bin), progress_callback):
            ok_cp, _, _ = _run_command(
                ["cp", "-f", sbctl_host, dest_bin],
                "Stage sbctl into target for chroot use",
                progress_callback,
                timeout=30,
            )
            if ok_cp:
                _run_command(["chmod", "755", dest_bin], "chmod sbctl", progress_callback, timeout=5)
                use_chroot = True

    if efi_partition_device:
        ok_m, err_m, _ = _efi_partition_ensure_mounted(
            target_root, efi_partition_device, progress_callback
        )
        if not ok_m:
            return False, err_m or "Failed to mount ESP for sbctl signing"

    if progress_callback:
        progress_callback("Creating Secure Boot keys (sbctl create-keys)...", None)

    if use_chroot:
        ok, err, _ = _run_in_chroot(
            target_root,
            ["sbctl", "create-keys"],
            "sbctl create-keys",
            progress_callback,
            timeout=120,
        )
    else:
        ok, err, _ = _run_command(
            [sbctl_host, "create-keys"],
            "sbctl create-keys",
            progress_callback,
            timeout=120,
        )
        if ok:
            for rel in ("var/lib/sbctl", "usr/share/secureboot"):
                src = "/" + rel
                if os.path.isdir(src):
                    dst = os.path.join(target_root, rel)
                    _ensure_directory(os.path.dirname(dst), progress_callback)
                    _run_command(
                        ["cp", "-a", src, dst],
                        f"Copy {rel} to target",
                        progress_callback,
                        timeout=60,
                    )
    if not ok:
        return False, err or "sbctl create-keys failed"

    vendor = _read_sys_vendor()
    oem_match = any(k in vendor for k in _SBCTL_OEM_VENDOR_KEYWORDS)
    if oem_match:
        enroll_cmd = ["sbctl", "enroll-keys", "-i", "--microsoft", "--firmware-builtin"]
        enroll_desc = (
            f"sbctl enroll-keys -i --microsoft --firmware-builtin "
            f"(OEM vendor match in '{vendor or 'unknown'}')"
        )
    else:
        enroll_cmd = ["sbctl", "enroll-keys", "-i", "--microsoft"]
        enroll_desc = (
            f"sbctl enroll-keys -i --microsoft "
            f"(standard mainboard fallback, vendor='{vendor or 'unknown'}')"
        )

    if progress_callback:
        progress_callback("Enrolling Secure Boot keys...", None)
    print(enroll_desc)

    if use_chroot:
        ok, err, _ = _run_in_chroot(
            target_root, enroll_cmd, enroll_desc, progress_callback, timeout=180
        )
    else:
        ok, err, _ = _run_command(
            [sbctl_host] + enroll_cmd[1:], enroll_desc, progress_callback, timeout=180
        )
    if not ok:
        return False, err or "sbctl enroll-keys failed"

    sign_targets = _collect_sbctl_sign_targets(
        target_root, efi_install_id or BOOTLOADER_ID, progress_callback
    )
    if not sign_targets:
        return False, "No PE bootloader/kernel targets found for sbctl sign"

    if progress_callback:
        progress_callback("Signing bootloader and kernel with sbctl...", None)

    for rel_path in sign_targets:
        abs_on_target = os.path.join(target_root, rel_path.lstrip("/"))
        if not os.path.isfile(abs_on_target):
            print(f"Warning: skip missing sign target {rel_path}")
            continue
        sign_cmd = ["sbctl", "sign", "-s", rel_path]
        if use_chroot:
            ok, err, _ = _run_in_chroot(
                target_root,
                sign_cmd,
                f"sbctl sign -s {rel_path}",
                progress_callback,
                timeout=180,
            )
        else:
            ok, err, _ = _run_command(
                [sbctl_host, "sign", "-s", abs_on_target],
                f"sbctl sign -s {abs_on_target}",
                progress_callback,
                timeout=180,
            )
        if not ok:
            return False, err or f"sbctl sign failed for {rel_path}"
        print(f"Signed with sbctl: {rel_path}")

    print("sbctl Secure Boot provisioning completed.")
    return True, ""


def _efi_partition_ensure_mounted(target_root, efi_partition_device, progress_callback=None):
    """Ensure the *target* EFI partition is mounted at target_root/boot/efi.
    If efi_partition_device is given, always use it (unmount and remount if something else is there)."""
    efi_mount = os.path.join(target_root, "boot", "efi")
    if not _ensure_directory(efi_mount, progress_callback):
        return False, "Failed to create EFI mount point", None

    def _realpath(dev):
        try:
            return os.path.realpath(dev) if dev else None
        except Exception:
            return dev

    if efi_partition_device:
        # Ensure the target's ESP is mounted here; avoid writing to host's ESP by mistake.
        want = _realpath(efi_partition_device)
        if os.path.ismount(efi_mount):
            try:
                r = subprocess.run(
                    ["findmnt", "-n", "-o", "SOURCE", "--target", efi_mount],
                    capture_output=True, text=True, check=False, timeout=10
                )
                current = _realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
                if current and want and current != want:
                    _run_command(["umount", efi_mount], "Unmount EFI for remount", progress_callback, timeout=15)
                elif current == want:
                    return True, "", efi_mount
            except Exception:
                pass
            if os.path.ismount(efi_mount):
                _run_command(["umount", efi_mount], "Unmount EFI", progress_callback, timeout=15)
        ok, err, _ = _run_command(
            ["mount", efi_partition_device, efi_mount],
            "Mount EFI partition", progress_callback, timeout=30
        )
        if not ok:
            return False, err or "Failed to mount EFI partition", None
        return True, "", efi_mount

    if os.path.ismount(efi_mount):
        return True, "", efi_mount
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", efi_mount],
            capture_output=True, text=True, check=False, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return True, "", efi_mount
    except Exception:
        pass
    return False, "UEFI system but EFI partition not mounted and no device provided.", None


def _normalize_findmnt_source(source):
    """Normalize findmnt SOURCE like /dev/sda3[/root] -> /dev/sda3."""
    if not source:
        return source
    s = source.strip()
    if s.startswith("/dev/") and "[" in s:
        s = s.split("[", 1)[0]
    return s


def _get_uuid_for_mount_target(target):
    """Return UUID for mounted target by UUID first, then SOURCE->blkid fallback."""
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "UUID", "--target", target],
            capture_output=True, text=True, check=False, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", target],
            capture_output=True, text=True, check=False, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            src = _normalize_findmnt_source(r.stdout.strip())
            if src.startswith("UUID="):
                return src.split("=", 1)[1].strip()
            if src.startswith("/dev/"):
                return _get_device_uuid(src)
    except Exception:
        pass

    return None


def _get_root_uuid(target_root):
    """Return UUID of the filesystem mounted at target_root (root partition)."""
    return _get_uuid_for_mount_target(target_root)


def _get_boot_uuid(target_root):
    """Return UUID for /boot mounted under target_root, when present."""
    return _get_uuid_for_mount_target(os.path.join(target_root, "boot"))


def _efi_file_readable(path):
    """Check if path exists, is a regular file, and has size > 0. Uses sudo for EFI partition access."""
    ok, _, _ = _run_command(["test", "-f", path, "-a", "-s", path], "Check EFI file", None, timeout=5)
    return ok


def _find_shim_grub_on_host():
    """Find shim and grub EFI files on host (live system) /boot/efi or /efi.
    Returns (shim_path, grub_path, efi_vendor). Uses architecture-specific file names (x64/aa64).
    Uses privileged check (sudo test) because /boot/efi may not be readable by liveuser."""
    arch = get_host_architecture()
    efi_shim = arch["efi_shim"]
    efi_grub = arch["efi_grub"]
    efi_boot = arch["efi_boot"]
    vendors = ["fedora", "centos", "rhel", "rocky", "almalinux", "oreon"]
    for efi_root in ["/boot/efi", "/efi"]:
        host_efi = os.path.join(efi_root, "EFI")
        ok, _, _ = _run_command(["test", "-d", host_efi], "Check EFI dir", None, timeout=5)
        if not ok:
            continue
        ok, _, ls_out = _run_command(["ls", "-1", host_efi], "List EFI dir", None, timeout=5)
        if not ok or not ls_out:
            continue
        names = [n.strip() for n in ls_out.splitlines() if n.strip()]
        shim = None
        grub = None
        efi_vendor = None
        for v in vendors:
            p = os.path.join(host_efi, v, efi_shim)
            if _efi_file_readable(p):
                shim = p
                efi_vendor = v
                break
        if not shim:
            for name in names:
                if name == "BOOT":
                    continue
                for f in (efi_shim, efi_boot):
                    p = os.path.join(host_efi, name, f)
                    if _efi_file_readable(p):
                        shim = p
                        efi_vendor = name
                        break
                if shim:
                    break
        if not shim:
            boot_dir = os.path.join(host_efi, "BOOT")
            for f in (efi_boot, efi_shim):
                p = os.path.join(boot_dir, f)
                if _efi_file_readable(p):
                    shim = p
                    break
        if not shim:
            continue
        for v in ([efi_vendor] if efi_vendor else vendors):
            p = os.path.join(host_efi, v, efi_grub)
            if _efi_file_readable(p):
                grub = p
                efi_vendor = efi_vendor or v
                break
        if not grub:
            p = os.path.join(host_efi, "BOOT", efi_grub)
            if _efi_file_readable(p):
                grub = p
        if not grub:
            for name in names:
                p = os.path.join(host_efi, name, efi_grub)
                if _efi_file_readable(p):
                    grub = p
                    efi_vendor = efi_vendor or name
                    break
        if shim and grub:
            return shim, grub, efi_vendor
    return None, None, None


def _get_device_uuid(device_path):
    """Return UUID of a block device (e.g. /dev/sda2)."""
    if not device_path:
        return None
    device = _normalize_findmnt_source(device_path)
    try:
        r = subprocess.run(
            ["blkid", "-o", "value", "-s", "UUID", device],
            capture_output=True, text=True, check=False, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _install_uefi_bootloader(target_root, primary_disk, efi_partition_device, progress_callback=None, boot_partition_device=None, offline_install=False, dual_boot=False, preserve_efi=False):
    """Install UEFI bootloader to match Anaconda/Oreon: EFI/<vendor> (e.g. almalinux),
    signed shim+grub from host, stub grub.cfg on ESP.
    Mounts the target ESP to a private temp dir so we always write to the correct partition.
    Dual boot + preserve EFI never overwrites an existing EFI/BOOT fallback (Windows)."""
    if not efi_partition_device:
        return False, "UEFI install requires the EFI partition device (e.g. /dev/sda1).", None
    if not os.path.exists(efi_partition_device):
        return False, "EFI partition device does not exist: %s" % efi_partition_device, None

    from backend import verify_grub_packages
    vok, verr, _ = verify_grub_packages(target_root, offline_install=offline_install)
    if not vok:
        return False, verr or "Required GRUB packages missing.", None

    shim_src, grub_src, efi_vendor = _find_shim_grub_on_host()
    if not shim_src or not grub_src:
        return False, "Host has no signed shim/grub in /boot/efi/EFI or /efi/EFI.", None

    arch = get_host_architecture()
    # Dual boot preserve: always use Oreon vendor dir so we dont collide with Windows
    if dual_boot and preserve_efi:
        efi_install_id = BOOTLOADER_ID
    else:
        efi_install_id = efi_vendor if efi_vendor else BOOTLOADER_ID
    tmp_mount = tempfile.mkdtemp(prefix="centrio_efi_")
    try:
        ok, err, _ = _run_command(
            ["mount", efi_partition_device, tmp_mount],
            "Mount ESP at temp dir", progress_callback, timeout=30
        )
        if not ok:
            return False, err or "Failed to mount ESP at temp dir", None

        efi_dir = os.path.join(tmp_mount, "EFI", efi_install_id)
        efi_boot = os.path.join(tmp_mount, "EFI", "BOOT")
        if not _ensure_directory(efi_dir, progress_callback):
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
            return False, "Failed to create EFI dirs on ESP", None

        host_vendor_dir = os.path.join("/boot/efi/EFI", efi_vendor or efi_install_id)
        ok_dir, _, _ = _run_command(["test", "-d", host_vendor_dir], "Check host EFI vendor dir", progress_callback, timeout=5)
        if not ok_dir:
            host_vendor_dir = os.path.join("/efi/EFI", efi_vendor or efi_install_id)
            ok_dir, _, _ = _run_command(["test", "-d", host_vendor_dir], "Check host EFI vendor dir", progress_callback, timeout=5)
        if ok_dir and not (dual_boot and preserve_efi):
            ok_ls, _, ls_out = _run_command(["ls", "-1", host_vendor_dir], "List host EFI vendor dir", progress_callback, timeout=5)
            if ok_ls and ls_out:
                for name in [n.strip() for n in ls_out.splitlines() if n.strip()]:
                    src = os.path.join(host_vendor_dir, name)
                    if _efi_file_readable(src):
                        ok, err, _ = _run_command(["cp", src, os.path.join(efi_dir, name)], f"Copy {name} to EFI", progress_callback)
                        if not ok:
                            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
                            return False, err or f"Failed to copy {name} from host EFI", None
        else:
            for s, d in [(shim_src, os.path.join(efi_dir, arch["efi_shim"])), (grub_src, os.path.join(efi_dir, arch["efi_grub"]))]:
                ok, err, _ = _run_command(["cp", s, d], "Copy shim/grub to EFI", progress_callback)
                if not ok:
                    _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
                    return False, err or "Failed to copy shim/grub", None
            ok, err, _ = _run_command(
                ["cp", shim_src, os.path.join(efi_dir, "bootx64.efi")],
                "Copy shim as bootx64.efi",
                progress_callback,
            )
            if not ok:
                print(f"Warning: could not stage bootx64.efi: {err}")

        bootx64_dest = os.path.join(efi_boot, arch["efi_boot"])
        existing_boot = os.path.isfile(bootx64_dest)
        microsoft_efi = os.path.isfile(
            os.path.join(tmp_mount, "EFI", "Microsoft", "Boot", "bootmgfw.efi")
        )
        skip_fallback = dual_boot and preserve_efi and (existing_boot or microsoft_efi)
        if skip_fallback:
            print(
                "Dual boot preserve EFI: leaving EFI/BOOT fallback untouched "
                "(existing OS boot files present)."
            )
        else:
            if not _ensure_directory(efi_boot, progress_callback):
                _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
                return False, "Failed to create EFI/BOOT on ESP", None
            ok, err, _ = _run_command(["cp", shim_src, bootx64_dest], "Copy shim to EFI/BOOT", progress_callback)
            if not ok:
                _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
                return False, err or "Failed to copy shim to EFI/BOOT", None
            ok, err, _ = _run_command(["cp", grub_src, os.path.join(efi_boot, arch["efi_grub"])], "Copy grub to EFI/BOOT", progress_callback)
            if not ok:
                _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
                return False, err or "Failed to copy grub to EFI/BOOT", None

        # When boot_partition_device given (separate /boot), use its UUID so GRUB reads from /boot partition
        if boot_partition_device:
            uuid = _get_device_uuid(boot_partition_device)
            if not uuid:
                uuid = _get_boot_uuid(target_root)
            prefix_path = "/grub2"  # /boot partition root has grub2/
        else:
            uuid = _get_root_uuid(target_root)
            prefix_path = "/boot/grub2"
        cfg_hint = "/grub2/grub.cfg" if prefix_path == "/grub2" else "/boot/grub2/grub.cfg"
        # Robust stub: prefer fs_uuid when available, but always include a file-based
        # fallback so installation remains bootable even when UUID detection is flaky in
        # installer mount states.
        if uuid:
            stub_cfg = (
                "search --no-floppy --fs-uuid --set=root %s\n"
                "if [ -z \"$root\" ]; then\n"
                "  search --no-floppy --file --set=root %s\n"
                "fi\n"
                "set prefix=($root)%s\n"
                "configfile $prefix/grub.cfg\n"
            ) % (uuid, cfg_hint, prefix_path)
        else:
            stub_cfg = (
                "search --no-floppy --file --set=root %s\n"
                "set prefix=($root)%s\n"
                "configfile $prefix/grub.cfg\n"
            ) % (cfg_hint, prefix_path)
        efi_grub_cfg = os.path.join(efi_dir, "grub.cfg")
        if not _write_file_as_root(efi_grub_cfg, stub_cfg, progress_callback):
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
            return False, "Failed to write stub grub.cfg on ESP", None

        try:
            os.sync()
        except Exception:
            pass
        _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
    finally:
        if os.path.ismount(tmp_mount):
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
        try:
            os.rmdir(tmp_mount)
        except Exception:
            pass

    # NVRAM: point to shim in vendor dir
    match = (re.match(r"(/dev/[a-zA-Z]+)(\d+)", efi_partition_device) or
            re.match(r"(/dev/nvme\d+n\d+)p(\d+)", efi_partition_device) or
            re.match(r"(/dev/mmcblk\d+)p(\d+)", efi_partition_device))
    if match:
        efi_disk, efi_part = match.group(1), match.group(2)
        arch = get_host_architecture()
        loader = "\\EFI\\" + efi_install_id + "\\" + arch["efi_shim"].replace("/", "\\")
        _run_command(
            ["efibootmgr", "-c", "-d", efi_disk, "-p", efi_part, "-L", efi_install_id, "-l", loader],
            "Add NVRAM boot entry", progress_callback, timeout=60
        )

    return True, "", efi_install_id


def _device_to_disk(device):
    """Return base disk path for grub2-install. /dev/sda2 -> /dev/sda, /dev/nvme0n1p2 -> /dev/nvme0n1."""
    if not device or not device.startswith("/dev/"):
        return device
    # nvme: /dev/nvme0n1p2 -> /dev/nvme0n1
    m = re.match(r"^(/dev/nvme\d+n\d+)p?\d*$", device)
    if m:
        return m.group(1)
    # mmcblk: /dev/mmcblk0p2 -> /dev/mmcblk0
    m = re.match(r"^(/dev/mmcblk\d+)p?\d*$", device)
    if m:
        return m.group(1)
    # sdX, vdX, xvdX: /dev/sda2 -> /dev/sda
    m = re.match(r"^(/dev/[a-z]+)\d*$", device)
    if m:
        return m.group(1)
    return device


def _install_bios_bootloader(target_root, primary_disk, progress_callback=None):
    """Install GRUB for legacy BIOS. Returns (success, error_msg). Not supported on ARM64.
    Runs grub2-install on the host (live) so it uses the live's /usr/lib/grub/i386-pc/;
    --boot-directory points at the target's /boot."""
    arch = get_host_architecture()
    if not arch.get("has_bios", True):
        return False, "Legacy BIOS bootloader not supported on ARM64 (UEFI only)."
    disk = _device_to_disk(primary_disk)
    from backend import _run_command
    boot_dir = os.path.join(target_root, "boot")
    ok, err, stdout = _run_command(
        ["grub2-install", "--target=i386-pc", "--force", "--recheck",
         "--boot-directory", boot_dir, disk],
        "grub2-install (BIOS)",
        progress_callback,
        timeout=180
    )
    if not ok:
        return False, f"grub2-install (BIOS) failed: {err or stdout}"
    return True, ""


def _get_live_root_uuid():
    """Return UUID of the live system's root filesystem (/)."""
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "UUID", "--target", "/"],
            capture_output=True, text=True, check=False, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _copy_grub_cfg_from_live_and_patch_uuid(target_root, target_root_uuid, progress_callback=None):
    """Copy /boot/grub2/grub.cfg from live env to target and replace live root UUID with target's.
    Uses sudo cat to read live file (may not be readable by liveuser)."""
    live_grub_cfg = "/boot/grub2/grub.cfg"
    cfg_path = os.path.join(target_root, "boot", "grub2", "grub.cfg")
    ok, _, content = _run_command(["cat", live_grub_cfg], "Read live grub.cfg", progress_callback, timeout=10)
    if not ok or not content or len(content.strip()) < 50:
        return False, "Live system has no usable /boot/grub2/grub.cfg to copy."
    live_uuid = _get_live_root_uuid()
    if not live_uuid:
        return False, "Could not determine live root UUID for grub.cfg patch."
    try:
        # Replace live root UUID with target root UUID (handles search.fs_uuid, root=UUID=..., etc.)
        content = content.replace(live_uuid, target_root_uuid)
        # Ensure quiet splash in kernel cmdline so Plymouth boot screen shows (not verbose log)
        lines_out = []
        for line in content.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("linux ") or stripped.startswith("linuxefi "):
                parts = stripped.split(None, 2)  # cmd, path, rest
                if len(parts) >= 3:
                    args = [a for a in parts[2].split()
                            if not a.startswith("resume=") and not a.startswith("rd.lvm.lv=")
                            and not a.startswith("rootflags=")]
                    for param in ["quiet", "splash", "rhgb", "rd.plymouth=1"]:
                        if param not in args:
                            args.append(param)
                    lines_out.append(parts[0] + " " + parts[1] + " " + " ".join(args))
                else:
                    lines_out.append(stripped)
            else:
                lines_out.append(stripped)
        content = "\n".join(lines_out) + "\n"
        if not _ensure_directory(os.path.dirname(cfg_path), progress_callback):
            return False, "Failed to create grub config directory."
        if not _write_file_as_root(cfg_path, content, progress_callback):
            return False, "Failed to write grub.cfg to target."
        if progress_callback:
            progress_callback("Transferred grub.cfg from live env and patched root UUID", None)
        print("Transferred grub.cfg from live env and patched root UUID.")
        return True, ""
    except Exception as e:
        return False, "Failed to copy/patch grub.cfg from live: %s" % e


def _ensure_windows_grub_entry(target_root, progress_callback=None):
    """If Microsoft bootmgfw.efi exists on the target ESP, make sure GRUB can chainload it."""
    bootmgfw = os.path.join(
        target_root, "boot", "efi", "EFI", "Microsoft", "Boot", "bootmgfw.efi"
    )
    if not os.path.isfile(bootmgfw):
        return False
    drop_in_dir = os.path.join(target_root, "etc", "grub.d")
    drop_in = os.path.join(drop_in_dir, "45_centrio_windows")
    esp_uuid = _get_uuid_for_mount_target(os.path.join(target_root, "boot", "efi"))
    if esp_uuid:
        script = (
            "#!/bin/sh\n"
            "exec tail -n +3 $0\n"
            "menuentry 'Windows Boot Manager' --class windows --class os {\n"
            "    insmod part_gpt\n"
            "    insmod fat\n"
            "    search --no-floppy --fs-uuid --set=root %s\n"
            "    chainloader /EFI/Microsoft/Boot/bootmgfw.efi\n"
            "}\n"
        ) % esp_uuid
    else:
        script = (
            "#!/bin/sh\n"
            "exec tail -n +3 $0\n"
            "menuentry 'Windows Boot Manager' --class windows --class os {\n"
            "    insmod part_gpt\n"
            "    insmod fat\n"
            "    search --no-floppy --file --set=root /EFI/Microsoft/Boot/bootmgfw.efi\n"
            "    chainloader /EFI/Microsoft/Boot/bootmgfw.efi\n"
            "}\n"
        )
    if not _ensure_directory(drop_in_dir, progress_callback):
        return False
    if not _write_file_as_root(drop_in, script, progress_callback):
        return False
    _run_command(["chmod", "755", drop_in], "chmod Windows GRUB drop-in", progress_callback, timeout=5)
    print("Added Centrio Windows Boot Manager GRUB entry for dual boot.")
    return True


def _patch_grub_default_os_prober(target_root, enable, progress_callback=None):
    """Set GRUB_DISABLE_OS_PROBER in target /etc/default/grub."""
    grub_default = os.path.join(target_root, "etc", "default", "grub")
    value = "false" if enable else "true"
    content = ""
    ok_cat, _, cat_out = _run_command(["cat", grub_default], "Read /etc/default/grub", progress_callback, timeout=5)
    if ok_cat and cat_out:
        content = cat_out
    if re.search(r"^GRUB_DISABLE_OS_PROBER=", content, re.MULTILINE):
        content = re.sub(
            r"^GRUB_DISABLE_OS_PROBER=.*$",
            f"GRUB_DISABLE_OS_PROBER={value}",
            content,
            flags=re.MULTILINE,
        )
    else:
        content = (content.rstrip() + f"\nGRUB_DISABLE_OS_PROBER={value}\n") if content else f"GRUB_DISABLE_OS_PROBER={value}\n"
    return _write_file_as_root(grub_default, content, progress_callback)


def _generate_grub_cfg(target_root, primary_disk, is_uefi, progress_callback=None, dual_boot=False):
    """Generate /boot/grub2/grub.cfg for target (must run inside chroot to see target's /boot). Returns (success, error_msg).
    Dual boot enables os-prober (with timeout). Otherwise disable it to avoid chroot hangs.
    If grub2-mkconfig produces empty/small output, falls back to copying grub.cfg from the live env and patching root UUID."""
    grub_cfg_chroot = "/boot/grub2/grub.cfg"
    cfg_path = os.path.join(target_root, "boot", "grub2", "grub.cfg")

    if dual_boot:
        _patch_grub_default_os_prober(target_root, enable=True, progress_callback=progress_callback)
        if is_uefi:
            # ESP must be mounted for Windows detection / drop-in
            _ensure_windows_grub_entry(target_root, progress_callback)
        os_prober_env = "false"
        mkconfig_timeout = 180
    else:
        _patch_grub_default_os_prober(target_root, enable=False, progress_callback=progress_callback)
        os_prober_env = "true"
        mkconfig_timeout = 120

    ok, err, _ = _run_in_chroot(
        target_root,
        ["env", f"GRUB_DISABLE_OS_PROBER={os_prober_env}", "grub2-mkconfig", "-o", grub_cfg_chroot],
        "grub2-mkconfig",
        progress_callback,
        timeout=mkconfig_timeout,
    )
    if not ok and dual_boot:
        print(f"Warning: dual-boot grub2-mkconfig with os-prober failed ({err}); retrying without os-prober.")
        ok, err, _ = _run_in_chroot(
            target_root,
            ["env", "GRUB_DISABLE_OS_PROBER=true", "grub2-mkconfig", "-o", grub_cfg_chroot],
            "grub2-mkconfig (no os-prober fallback)",
            progress_callback,
            timeout=120,
        )
    if not ok:
        target_root_uuid = _get_root_uuid(target_root)
        if target_root_uuid:
            ok2, err2 = _copy_grub_cfg_from_live_and_patch_uuid(target_root, target_root_uuid, progress_callback)
            if ok2:
                return True, ""
        return False, err or "grub2-mkconfig failed."

    ok_stat, _, size_out = _run_command(["stat", "-c", "%s", cfg_path], "Check grub.cfg size", progress_callback, timeout=5)
    if ok_stat and size_out and size_out.strip().isdigit() and int(size_out.strip()) >= 100:
        return True, ""

    target_root_uuid = _get_root_uuid(target_root)
    if not target_root_uuid:
        return False, "GRUB config missing or too small and could not get target root UUID."
    ok2, err2 = _copy_grub_cfg_from_live_and_patch_uuid(target_root, target_root_uuid, progress_callback)
    if ok2:
        return True, ""
    return False, "GRUB config missing or too small after grub2-mkconfig; fallback failed: %s" % err2


def install_bootloader(target_root, primary_disk, efi_partition_device, progress_callback=None, boot_partition_device=None, offline_install=False, dual_boot=False, preserve_efi=False):
    """
    Install bootloader for target: UEFI (with Secure Boot support) or legacy BIOS.
    Works with dnf-based systems. Returns (success, error_msg, verification_dict or None).
    """
    if not primary_disk:
        return False, "No primary disk specified.", None

    uefi = is_uefi_system()
    if progress_callback:
        progress_callback("Installing bootloader (%s)..." % ("UEFI" if uefi else "BIOS"), None)

    efi_install_id = BOOTLOADER_ID
    if uefi:
        ok, err, efi_install_id = _install_uefi_bootloader(
            target_root, primary_disk, efi_partition_device, progress_callback,
            boot_partition_device=boot_partition_device,
            offline_install=offline_install,
            dual_boot=dual_boot,
            preserve_efi=preserve_efi,
        )
        if efi_install_id is None:
            efi_install_id = BOOTLOADER_ID
    else:
        ok, err = _install_bios_bootloader(target_root, primary_disk, progress_callback)

    if not ok:
        return False, err, None

    # Remount ESP at target before grub.cfg / Windows detection for dual boot
    if uefi and efi_partition_device:
        _efi_partition_ensure_mounted(target_root, efi_partition_device, progress_callback)

    ok, err = _generate_grub_cfg(
        target_root, primary_disk, uefi, progress_callback, dual_boot=dual_boot
    )
    if not ok:
        return False, err, None

    if uefi:
        if progress_callback:
            progress_callback("Provisioning Secure Boot with sbctl...", None)
        ok_sb, err_sb = provision_secure_boot_with_sbctl(
            target_root,
            efi_partition_device=efi_partition_device,
            efi_install_id=efi_install_id,
            progress_callback=progress_callback,
        )
        if not ok_sb:
            return False, err_sb or "Secure Boot provisioning with sbctl failed", None

    verification = {
        "uefi": uefi,
        "bootloader_id": efi_install_id if uefi else BOOTLOADER_ID,
        "primary_disk": primary_disk,
        "efi_partition": efi_partition_device if uefi else None,
        "dual_boot": dual_boot,
    }
    return True, "", verification
