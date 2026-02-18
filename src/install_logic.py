# centrio_installer/install_logic.py
# Backend for bootloader installation (UEFI and BIOS).
#
# How distros typically do UEFI GRUB (no grub2-install for EFI):
# - Fedora/RHEL (Anaconda): Does NOT run grub2-install for UEFI. Requires packages
#   grub2-efi-x64 and shim-x64; the RPMs install signed shim/grub to the target.
#   Installer runs gen_grub_cfgstub, grub2-mkconfig, and efibootmgr to add NVRAM
#   entry pointing at shimx64.efi. See pyanaconda bootloader/efi.py (EFIGRUB).
# - Calamares: Uses grub-install for EFI (can hit "should not be used for EFI" and
#   may use --force or distro-specific paths).
# - Debian: grub-efi has hardcoded id "debian"; must use EFI/debian and stub grub.cfg.
#
# We follow the Fedora model: use distro signed shim + grub (from target), write
# grub.cfg, register with efibootmgr. No grub2-install for UEFI.

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
    """Ensure EFI partition is mounted at target_root/boot/efi. Mount if not. Returns (success, err, efi_mount_point)."""
    efi_mount = os.path.join(target_root, "boot", "efi")
    try:
        os.makedirs(efi_mount, exist_ok=True)
    except Exception as e:
        return False, f"Failed to create EFI mount point: {e}", None

    if os.path.ismount(efi_mount):
        return True, "", efi_mount

    if not efi_partition_device:
        # Try findmnt
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

    r = subprocess.run(
        ["mount", efi_partition_device, efi_mount],
        capture_output=True, text=True, check=False, timeout=30
    )
    if r.returncode != 0:
        return False, f"Failed to mount EFI partition: {r.stderr.strip()}", None
    return True, "", efi_mount


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
    """Install GRUB for UEFI (and Secure Boot via shim). Returns (success, error_msg, efi_install_id)."""
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

    # Ensure GRUB EFI packages in target (dnf-based)
    from backend import verify_grub_packages
    vok, verr, _ = verify_grub_packages(target_root)
    if not vok:
        return False, verr or "Required GRUB packages missing.", None

    shim_src, _, host_efi_vendor = _find_shim_grub_on_host()
    if not shim_src:
        return False, "Could not find shim (shimx64.efi/BOOTX64.EFI) on live system; required for UEFI/Secure Boot.", None

    # Signed grub: target /usr, target ESP, or host ESP. Use same EFI dir as grub source so embedded prefix finds grub.cfg.
    signed_grub_paths = [
        os.path.join(target_root, "usr/lib/grub/x86_64-efi/grubx64.efi"),
        os.path.join(target_root, "usr/lib/grub2/x86_64-efi/grubx64.efi"),
        os.path.join(target_root, "usr/share/grub/x86_64-efi/grubx64.efi"),
    ]
    for base, efi_sub in [(os.path.join(target_root, "boot", "efi"), "EFI"), ("/boot/efi", "EFI")]:
        efi_efi = os.path.join(base, efi_sub)
        if os.path.isdir(efi_efi):
            for name in os.listdir(efi_efi):
                cand = os.path.join(efi_efi, name, "grubx64.efi")
                if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                    signed_grub_paths.append(cand)
                    break
    grub_src_used = None
    for p in signed_grub_paths:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            grub_src_used = p
            break
    if not grub_src_used:
        return False, (
            "Signed GRUB (grubx64.efi) not found. Checked: target usr/lib/grub*, usr/share/grub*, "
            "target and host boot/efi/EFI/*/. Install grub2-efi-x64 or ensure live ESP has grubx64.efi."
        ), None
    # Embedded prefix in distro grub points at (hd0,gptN)/EFI/<vendor>/. Use that dir so grub finds grub.cfg.
    efi_install_id = BOOTLOADER_ID
    if "/EFI/" in grub_src_used:
        parts = grub_src_used.split("/EFI/", 1)[-1].strip("/").split("/")
        if parts and parts[0] != "BOOT":
            efi_install_id = parts[0]
    elif host_efi_vendor:
        efi_install_id = host_efi_vendor
    efi_dir = os.path.join(efi_mount_point, "EFI", efi_install_id)
    efi_boot = os.path.join(efi_mount_point, "EFI", "BOOT")
    try:
        os.makedirs(efi_dir, exist_ok=True)
        os.makedirs(efi_boot, exist_ok=True)
    except Exception as e:
        return False, f"Failed to create EFI dirs: {e}", None

    shim_dst = os.path.join(efi_dir, "shimx64.efi")
    bootx64_dst = os.path.join(efi_dir, "BOOTX64.EFI")
    grub_dst = os.path.join(efi_dir, "grubx64.efi")
    try:
        shutil.copy(shim_src, shim_dst)
        shutil.copy(shim_src, bootx64_dst)
        if os.path.normpath(grub_src_used) != os.path.normpath(grub_dst):
            shutil.copy(grub_src_used, grub_dst)
    except Exception as e:
        return False, f"Failed to copy shim/grub: {e}", None

    efi_boot_shim = os.path.join(efi_boot, "BOOTX64.EFI")
    shutil.copy(shim_src, efi_boot_shim)

    if efi_partition_device:
        match = (re.match(r"(/dev/[a-zA-Z]+)(\d+)", efi_partition_device) or
                re.match(r"(/dev/nvme\d+n\d+)p(\d+)", efi_partition_device) or
                re.match(r"(/dev/mmcblk\d+)p(\d+)", efi_partition_device))
        if match:
            efi_disk, efi_part = match.group(1), match.group(2)
            loader = "\\EFI\\" + efi_install_id + "\\BOOTX64.EFI"
            cmd = ["efibootmgr", "-c", "-d", efi_disk, "-p", efi_part, "-L", efi_install_id, "-l", loader]
            subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)

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

    # Common: generate grub.cfg
    ok, err = _generate_grub_cfg(target_root, primary_disk, uefi, progress_callback)
    if not ok:
        return False, err, None

    # Copy grub.cfg to EFI partition (same dir as grub binary so embedded prefix finds it)
    if uefi and efi_install_id:
        efi_mount = os.path.join(target_root, "boot", "efi")
        cfg_src = os.path.join(target_root, "boot", "grub2", "grub.cfg")
        efi_cfg = os.path.join(efi_mount, "EFI", efi_install_id, "grub.cfg")
        if os.path.ismount(efi_mount) and os.path.exists(cfg_src) and os.path.exists(os.path.dirname(efi_cfg)):
            try:
                shutil.copy(cfg_src, efi_cfg)
            except Exception as e:
                print(f"Warning: Could not copy grub.cfg to EFI: {e}")

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
