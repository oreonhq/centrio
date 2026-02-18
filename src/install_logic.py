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

# Helpers from backend (imported at use site to avoid circular deps)
def _run_command(command_list, description, progress_callback=None, timeout=None, pipe_input=None):
    """Delegate to backend._run_command."""
    from backend import _run_command as _rc
    return _rc(command_list, description, progress_callback, timeout, pipe_input)

def _run_in_chroot(target_root, command_list, description, progress_callback=None, timeout=None, pipe_input=None):
    """Delegate to backend._run_in_chroot."""
    from backend import _run_in_chroot as _rch
    return _rch(target_root, command_list, description, progress_callback, timeout, pipe_input)


BOOTLOADER_ID = "Oreon"

# --- UEFI and BIOS detection ---
def is_uefi_system():
    return os.path.exists("/sys/firmware/efi")


def _efi_partition_ensure_mounted(target_root, efi_partition_device):
    """Ensure the *target* EFI partition is mounted at target_root/boot/efi.
    If efi_partition_device is given, always use it (unmount and remount if something else is there)."""
    efi_mount = os.path.join(target_root, "boot", "efi")
    try:
        os.makedirs(efi_mount, exist_ok=True)
    except Exception as e:
        return False, f"Failed to create EFI mount point: {e}", None

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
                    subprocess.run(["umount", efi_mount], capture_output=True, text=True, timeout=15)
                elif current == want:
                    return True, "", efi_mount
            except Exception:
                pass
            if os.path.ismount(efi_mount):
                try:
                    subprocess.run(["umount", efi_mount], capture_output=True, text=True, timeout=15)
                except Exception:
                    pass
        r = subprocess.run(
            ["mount", efi_partition_device, efi_mount],
            capture_output=True, text=True, check=False, timeout=30
        )
        if r.returncode != 0:
            return False, f"Failed to mount EFI partition: {r.stderr.strip()}", None
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


def _find_shim_grub_on_host():
    """Find shim and grub EFI files on host (live system). Returns (shim_path, grub_path, efi_vendor).
    efi_vendor is the EFI subdir name (e.g. 'almalinux') when found under /boot/efi/EFI/<name>/, else None.
    """
    host_efi = "/boot/efi/EFI"
    shim = None
    grub = None
    efi_vendor = None
    # Fixed paths
    for p in [
        "/boot/efi/EFI/BOOT/BOOTX64.EFI", "/boot/efi/EFI/BOOT/shimx64.efi",
        "/boot/efi/EFI/fedora/shimx64.efi", "/boot/efi/EFI/centos/shimx64.efi",
        "/boot/efi/EFI/rhel/shimx64.efi", "/boot/efi/EFI/rocky/shimx64.efi",
        "/boot/efi/EFI/almalinux/shimx64.efi", "/boot/efi/EFI/oreon/shimx64.efi",
        "/boot/shimx64.efi", "/boot/BOOTX64.EFI",
    ]:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            shim = p
            break
    if not shim and os.path.isdir(host_efi):
        for name in os.listdir(host_efi):
            for f in ("shimx64.efi", "BOOTX64.EFI"):
                p = os.path.join(host_efi, name, f)
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    shim = p
                    efi_vendor = name
                    break
            if shim:
                break
    if shim and not efi_vendor and host_efi in shim:
        parts = shim.replace(host_efi, "").strip("/").split("/")
        if len(parts) >= 1 and parts[0] != "BOOT":
            efi_vendor = parts[0]
    for p in [
        "/boot/efi/EFI/BOOT/grubx64.efi", "/boot/efi/EFI/fedora/grubx64.efi",
        "/boot/efi/EFI/centos/grubx64.efi", "/boot/efi/EFI/rhel/grubx64.efi",
        "/boot/efi/EFI/rocky/grubx64.efi", "/boot/efi/EFI/almalinux/grubx64.efi",
        "/boot/efi/EFI/oreon/grubx64.efi", "/boot/grubx64.efi",
    ]:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            grub = p
            if not efi_vendor and host_efi in p:
                parts = p.replace(host_efi, "").strip("/").split("/")
                if len(parts) >= 1 and parts[0] != "BOOT":
                    efi_vendor = parts[0]
            break
    if not grub and os.path.isdir(host_efi):
        for name in os.listdir(host_efi):
            p = os.path.join(host_efi, name, "grubx64.efi")
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                grub = p
                efi_vendor = name
                break
    return shim, grub, efi_vendor


def _install_uefi_bootloader(target_root, primary_disk, efi_partition_device, progress_callback=None):
    """Install UEFI bootloader to match Anaconda/Oreon: EFI/<vendor> (e.g. almalinux),
    signed shim+grub from host, stub grub.cfg on ESP. Returns (success, error_msg, efi_install_id)."""
    efi_mount_point = os.path.join(target_root, "boot", "efi")
    if not efi_partition_device and os.path.ismount(efi_mount_point):
        try:
            r = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE", "--target", efi_mount_point],
                capture_output=True, text=True, check=False, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                efi_partition_device = r.stdout.strip()
        except Exception:
            pass

    ok, err, _ = _efi_partition_ensure_mounted(target_root, efi_partition_device)
    if not ok:
        return False, err or "EFI partition not available.", None

    from backend import verify_grub_packages
    vok, verr, _ = verify_grub_packages(target_root)
    if not vok:
        return False, verr or "Required GRUB packages missing.", None

    shim_src, grub_src, efi_vendor = _find_shim_grub_on_host()
    if not shim_src or not grub_src:
        return False, "Host has no signed shim/grub in /boot/efi/EFI (need e.g. EFI/almalinux).", None

    # Match Anaconda layout: use vendor dir (almalinux) so layout matches working Oreon installs.
    efi_install_id = efi_vendor if efi_vendor else "almalinux"
    efi_dir = os.path.join(efi_mount_point, "EFI", efi_install_id)
    efi_boot = os.path.join(efi_mount_point, "EFI", "BOOT")
    try:
        os.makedirs(efi_dir, exist_ok=True)
        os.makedirs(efi_boot, exist_ok=True)
    except Exception as e:
        return False, f"Failed to create EFI dirs: {e}", None

    # Copy full vendor dir from host so we get shim, grub, and any .efi/.CSV (like Anaconda).
    host_vendor_dir = os.path.join("/boot/efi/EFI", efi_install_id)
    if os.path.isdir(host_vendor_dir):
        for name in os.listdir(host_vendor_dir):
            src = os.path.join(host_vendor_dir, name)
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, os.path.join(efi_dir, name))
                except Exception as e:
                    return False, f"Failed to copy {name} from host EFI: {e}", None
    else:
        # Fallback: copy only shim and grub we found.
        try:
            shutil.copy2(shim_src, os.path.join(efi_dir, "shimx64.efi"))
            shutil.copy2(grub_src, os.path.join(efi_dir, "grubx64.efi"))
        except Exception as e:
            return False, f"Failed to copy shim/grub: {e}", None

    # Fallback boot: EFI/BOOT/BOOTX64.EFI (shim).
    try:
        shutil.copy2(shim_src, os.path.join(efi_boot, "BOOTX64.EFI"))
    except Exception as e:
        return False, f"Failed to copy shim to EFI/BOOT: {e}", None

    root_uuid = _get_root_uuid(target_root)
    if not root_uuid:
        return False, "Could not determine root filesystem UUID for GRUB stub.", None

    # Stub grub.cfg on ESP (UnifyGrubConfig style): find root by UUID, load real config from /boot/grub2.
    stub_cfg = (
        "search.fs_uuid %s root\nset prefix=($root)/boot/grub2\nconfigfile $prefix/grub.cfg\n"
        % root_uuid
    )
    efi_grub_cfg = os.path.join(efi_dir, "grub.cfg")
    try:
        with open(efi_grub_cfg, "w") as f:
            f.write(stub_cfg)
    except Exception as e:
        return False, "Failed to write stub grub.cfg on ESP: %s" % e, None

    # Verify vendor dir on ESP so we never leave it missing.
    if not os.path.isdir(efi_dir):
        return False, "EFI/%s was not created on ESP." % efi_install_id, None
    has_cfg = os.path.isfile(os.path.join(efi_dir, "grub.cfg"))
    has_shim = os.path.isfile(os.path.join(efi_dir, "shimx64.efi"))
    has_grub = os.path.isfile(os.path.join(efi_dir, "grubx64.efi"))
    if not has_cfg or (not has_shim and not has_grub):
        return False, "ESP missing EFI/%s/grub.cfg or shim/grub. Check that target ESP was mounted." % efi_install_id, None

    # NVRAM: point to shim in vendor dir (AlmaLinux/Oreon use shimx64.efi there).
    if efi_partition_device:
        match = (re.match(r"(/dev/[a-zA-Z]+)(\d+)", efi_partition_device) or
                re.match(r"(/dev/nvme\d+n\d+)p(\d+)", efi_partition_device) or
                re.match(r"(/dev/mmcblk\d+)p(\d+)", efi_partition_device))
        if match:
            efi_disk, efi_part = match.group(1), match.group(2)
            loader = "\\EFI\\" + efi_install_id + "\\shimx64.efi"
            subprocess.run(
                ["efibootmgr", "-c", "-d", efi_disk, "-p", efi_part, "-L", efi_install_id, "-l", loader],
                capture_output=True, text=True, check=False, timeout=60
            )

    return True, "", efi_install_id


def _install_bios_bootloader(target_root, primary_disk, progress_callback=None):
    """Install GRUB for legacy BIOS. Returns (success, error_msg)."""
    from backend import _run_in_chroot, _run_command
    # Ensure grub2-pc (and deps) in target
    for pkg in ["grub2-pc", "grub2-common", "grub2-tools"]:
        r = subprocess.run(["rpm", "-q", pkg, f"--root={target_root}"], capture_output=True, text=True, check=False, timeout=10)
        if r.returncode != 0:
            ok, err, _ = _run_in_chroot(
                target_root,
                ["dnf", "install", "-y", pkg],
                f"Install {pkg}",
                progress_callback,
                timeout=180
            )
            if not ok:
                return False, f"Missing {pkg}: {err}"

    boot_dir = os.path.join(target_root, "boot")
    cmd = [
        "grub2-install", "--target=i386-pc", "--force", "--recheck",
        "--boot-directory", boot_dir,
        primary_disk
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180)
    if r.returncode != 0:
        return False, f"grub2-install (BIOS) failed: {r.stderr.strip() or r.stdout}"
    return True, ""


def _generate_grub_cfg(target_root, primary_disk, is_uefi, progress_callback=None):
    """Generate /boot/grub2/grub.cfg for target (must run inside chroot to see target's /boot). Returns (success, error_msg)."""
    grub_cfg_chroot = "/boot/grub2/grub.cfg"
    ok, err, _ = _run_in_chroot(
        target_root,
        ["grub2-mkconfig", "-o", grub_cfg_chroot],
        "grub2-mkconfig",
        progress_callback,
        timeout=120
    )
    if not ok:
        return False, err or "grub2-mkconfig failed."

    cfg_path = os.path.join(target_root, "boot", "grub2", "grub.cfg")
    if not os.path.exists(cfg_path) or os.path.getsize(cfg_path) < 100:
        return False, "GRUB config missing or too small."
    return True, ""


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

    # Optional: regenerate initramfs (best effort)
    try:
        vmlinuz_dir = os.path.join(target_root, "boot")
        if os.path.exists(vmlinuz_dir):
            kernels = sorted([f for f in os.listdir(vmlinuz_dir) if f.startswith("vmlinuz-") and "rescue" not in f])
            for k in reversed(kernels):
                kver = k.replace("vmlinuz-", "")
                _run_in_chroot(target_root, ["dracut", "--force", "--kver", kver], f"dracut {kver}", progress_callback, timeout=300)
    except Exception as e:
        print(f"Warning: initramfs regeneration: {e}")

    verification = {
        "uefi": uefi,
        "bootloader_id": efi_install_id if uefi else BOOTLOADER_ID,
        "primary_disk": primary_disk,
        "efi_partition": efi_partition_device if uefi else None,
    }
    return True, "", verification
