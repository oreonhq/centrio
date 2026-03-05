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

# --- UEFI and BIOS detection ---
def is_uefi_system():
    return os.path.exists("/sys/firmware/efi")


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


def _get_root_uuid(target_root):
    """Return UUID of the filesystem mounted at target_root (root partition)."""
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "UUID", "--target", target_root],
            capture_output=True, text=True, check=False, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


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


def _install_uefi_bootloader(target_root, primary_disk, efi_partition_device, progress_callback=None):
    """Install UEFI bootloader to match Anaconda/Oreon: EFI/<vendor> (e.g. almalinux),
    signed shim+grub from host, stub grub.cfg on ESP.
    Mounts the target ESP to a private temp dir so we always write to the correct partition."""
    if not efi_partition_device:
        return False, "UEFI install requires the EFI partition device (e.g. /dev/sda1).", None
    if not os.path.exists(efi_partition_device):
        return False, "EFI partition device does not exist: %s" % efi_partition_device, None

    from backend import verify_grub_packages
    vok, verr, _ = verify_grub_packages(target_root)
    if not vok:
        return False, verr or "Required GRUB packages missing.", None

    shim_src, grub_src, efi_vendor = _find_shim_grub_on_host()
    if not shim_src or not grub_src:
        return False, "Host has no signed shim/grub in /boot/efi/EFI or /efi/EFI.", None

    arch = get_host_architecture()
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
        if not _ensure_directory(efi_dir, progress_callback) or not _ensure_directory(efi_boot, progress_callback):
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
            return False, "Failed to create EFI dirs on ESP", None

        host_vendor_dir = os.path.join("/boot/efi/EFI", efi_install_id)
        ok_dir, _, _ = _run_command(["test", "-d", host_vendor_dir], "Check host EFI vendor dir", progress_callback, timeout=5)
        if not ok_dir:
            host_vendor_dir = os.path.join("/efi/EFI", efi_install_id)
            ok_dir, _, _ = _run_command(["test", "-d", host_vendor_dir], "Check host EFI vendor dir", progress_callback, timeout=5)
        if ok_dir:
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

        ok, err, _ = _run_command(["cp", shim_src, os.path.join(efi_boot, arch["efi_boot"])], "Copy shim to EFI/BOOT", progress_callback)
        if not ok:
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
            return False, err or "Failed to copy shim to EFI/BOOT", None

        root_uuid = _get_root_uuid(target_root)
        if not root_uuid:
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
            return False, "Could not determine root filesystem UUID for GRUB stub.", None

        stub_cfg = (
            "search.fs_uuid %s root\nset prefix=($root)/boot/grub2\nconfigfile $prefix/grub.cfg\n"
            % root_uuid
        )
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


def _generate_grub_cfg(target_root, primary_disk, is_uefi, progress_callback=None):
    """Generate /boot/grub2/grub.cfg for target (must run inside chroot to see target's /boot). Returns (success, error_msg).
    GRUB_DISABLE_OS_PROBER=true avoids os-prober scanning block devices in chroot, which can hang indefinitely.
    If grub2-mkconfig produces empty/small output, falls back to copying grub.cfg from the live env and patching root UUID."""
    grub_cfg_chroot = "/boot/grub2/grub.cfg"
    cfg_path = os.path.join(target_root, "boot", "grub2", "grub.cfg")

    ok, err, _ = _run_in_chroot(
        target_root,
        ["env", "GRUB_DISABLE_OS_PROBER=true", "grub2-mkconfig", "-o", grub_cfg_chroot],
        "grub2-mkconfig",
        progress_callback
    )
    if not ok:
        # Fall back to copying from live env
        target_root_uuid = _get_root_uuid(target_root)
        if target_root_uuid:
            ok2, err2 = _copy_grub_cfg_from_live_and_patch_uuid(target_root, target_root_uuid, progress_callback)
            if ok2:
                return True, ""
        return False, err or "grub2-mkconfig failed."

    ok_stat, _, size_out = _run_command(["stat", "-c", "%s", cfg_path], "Check grub.cfg size", progress_callback, timeout=5)
    if ok_stat and size_out and size_out.strip().isdigit() and int(size_out.strip()) >= 100:
        return True, ""

    # grub2-mkconfig produced empty or too-small output; fall back to live env
    target_root_uuid = _get_root_uuid(target_root)
    if not target_root_uuid:
        return False, "GRUB config missing or too small and could not get target root UUID."
    ok2, err2 = _copy_grub_cfg_from_live_and_patch_uuid(target_root, target_root_uuid, progress_callback)
    if ok2:
        return True, ""
    return False, "GRUB config missing or too small after grub2-mkconfig; fallback failed: %s" % err2


def install_bootloader(target_root, primary_disk, efi_partition_device, progress_callback=None):
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
        ok, err, efi_install_id = _install_uefi_bootloader(target_root, primary_disk, efi_partition_device, progress_callback)
        if efi_install_id is None:
            efi_install_id = BOOTLOADER_ID
    else:
        ok, err = _install_bios_bootloader(target_root, primary_disk, progress_callback)

    if not ok:
        return False, err, None

    # Common: generate grub.cfg on root fs at /boot/grub2/grub.cfg (standard location).
    ok, err = _generate_grub_cfg(target_root, primary_disk, uefi, progress_callback)
    if not ok:
        return False, err, None

    # Skip dracut regeneration for live copy, the copied initramfs is already valid.
    # BLS entries were patched with correct root=UUID. Plymouth is in the copied initramfs.
    # Regenerating in chroot can hang (udev/systemd probing); the copy boots fine without it.

    verification = {
        "uefi": uefi,
        "bootloader_id": efi_install_id if uefi else BOOTLOADER_ID,
        "primary_disk": primary_disk,
        "efi_partition": efi_partition_device if uefi else None,
    }
    return True, "", verification
