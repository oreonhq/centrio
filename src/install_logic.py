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
import struct
import subprocess
import shlex
import tempfile
import time
import uuid

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

_SETUPMODE_EFIVAR_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
_OREON_SB_OWNER_GUID = "6f72656f-6e2d-5342-2d6b-657973000001"
_KERNEL_KEYS_REL = "usr/share/doc/kernel-keys"


def is_uefi_system():
    return os.path.exists("/sys/firmware/efi")


def _find_host_or_target_tool(name, target_root=None):
    for candidate in (f"/usr/bin/{name}", f"/usr/sbin/{name}", f"/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    which = shutil.which(name)
    if which:
        return which
    if target_root:
        for rel in (f"usr/bin/{name}", f"usr/sbin/{name}", f"bin/{name}"):
            p = os.path.join(target_root, rel)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def _ensure_host_tool(name, target_root, progress_callback=None):
    found = _find_host_or_target_tool(name, None)
    if found:
        return found
    if not target_root:
        return None
    src = None
    for rel in (f"usr/bin/{name}", f"usr/sbin/{name}", f"bin/{name}"):
        p = os.path.join(target_root, rel)
        if os.path.isfile(p):
            src = p
            break
    if not src:
        return None
    dest = f"/usr/bin/{name}"
    ok, _, _ = _run_command(["cp", "-a", src, dest], f"Install {name} onto live system", progress_callback, timeout=30)
    if not ok or not os.path.isfile(dest):
        return None
    return dest


def _is_secure_boot_setup_mode(progress_callback=None):
    if not is_uefi_system():
        return False
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


def _kernel_keys_uki_names():
    arch = get_host_architecture().get("arch", "").lower()
    if arch in ("aarch64", "arm64"):
        return ("secureboot-uki-aa64.cer", "secureboot-uki-aarch64.cer", "secureboot-uki.cer")
    return ("secureboot-uki-x86_64.cer", "secureboot-uki.cer")


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        ok, _, out = _run_command(["ls", "-1", path], f"ls {path}", timeout=10)
        if not ok or not out:
            return []
        return [n.strip() for n in out.splitlines() if n.strip()]


def _list_kernel_key_dirs(root=""):
    base = os.path.join(root, _KERNEL_KEYS_REL) if root else os.path.join("/", _KERNEL_KEYS_REL)
    names = _listdir(base)
    out = []
    for n in names:
        if not n or n.startswith("."):
            continue
        p = os.path.join(base, n)
        ok, _, _ = _run_command(["test", "-d", p], f"test dir {n}", timeout=5)
        if ok:
            out.append(p)
    out.sort(reverse=True)
    return out


def _find_kernel_sb_certs(root=""):
    uki_names = _kernel_keys_uki_names()
    ca_names = (
        "kernel-signing-ca.cer",
        "oreonsecurebootca.cer",
        "oreonsecureboot501.cer",
    )
    for key_dir in _list_kernel_key_dirs(root):
        names = set(_listdir(key_dir))
        uki = next((os.path.join(key_dir, n) for n in uki_names if n in names), None)
        ca = next((os.path.join(key_dir, n) for n in ca_names if n in names), None)
        if uki or ca:
            return uki, ca
    return None, None


def _all_kernel_key_certs(root=""):
    out = []
    exts = (".cer", ".crt", ".der", ".pem")
    for key_dir in _list_kernel_key_dirs(root):
        for n in _listdir(key_dir):
            if n.lower().endswith(exts):
                out.append(os.path.join(key_dir, n))
    return out


def _run_as_user(command_list, description, progress_callback=None, timeout=30):
    if progress_callback:
        progress_callback(description)
    try:
        proc = subprocess.run(
            command_list,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except Exception as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
    return True, ""


def _openssl_bin():
    return shutil.which("openssl") or "/usr/bin/openssl"


def _cer_to_pem(cer_path, pem_path, progress_callback=None):
    openssl = _openssl_bin()
    if not os.path.isfile(openssl):
        return False, "openssl not found"
    src = cer_path
    try:
        with open(cer_path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return False, str(e)
    staged = pem_path + ".cer"
    with open(staged, "wb") as fh:
        fh.write(raw)
    src = staged
    for args in (
        [openssl, "x509", "-inform", "DER", "-in", src, "-outform", "PEM", "-out", pem_path],
        [openssl, "x509", "-in", src, "-outform", "PEM", "-out", pem_path],
    ):
        ok, err = _run_as_user(args, f"PEM {os.path.basename(cer_path)}", progress_callback)
        if ok and os.path.isfile(pem_path):
            return True, ""
    return False, err or f"Failed to convert {cer_path} to PEM"


def _clear_secure_boot_efivar_immutable(progress_callback=None):
    efivars = "/sys/firmware/efi/efivars"
    try:
        names = os.listdir(efivars)
    except OSError:
        return
    prefixes = ("db-", "KEK-", "PK-", "dbx-")
    for name in names:
        if not name.startswith(prefixes):
            continue
        path = os.path.join(efivars, name)
        _run_command(
            ["chattr", "-i", path],
            f"chattr -i {name}",
            progress_callback,
            timeout=5,
        )


def _cert_to_esl(cert_to_efi, pem_path, esl_path, progress_callback=None):
    args = [cert_to_efi, "-g", _OREON_SB_OWNER_GUID, pem_path, esl_path]
    label = f"cert-to-efi-sig-list {os.path.basename(pem_path)}"
    ok, err = _run_as_user(args, label, progress_callback)
    if not ok or not os.path.isfile(esl_path):
        return False, err or f"cert-to-efi-sig-list failed for {pem_path}"
    return True, ""


_EFI_GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
_EFI_IMAGE_SECURITY_GUID = "d719b2cb-3d3a-4596-a3bc-dad00e67656f"
_EFI_CERT_TYPE_PKCS7_GUID = "4aafd29d-68df-49ee-8aa9-347d375665a7"
_WIN_CERT_TYPE_EFI_GUID = 0x0EF1
_EFIVAR_NV_BS_RT_AT = 0x00000027
_EFIVAR_GUID = {
    "PK": _EFI_GLOBAL_GUID,
    "KEK": _EFI_GLOBAL_GUID,
    "db": _EFI_IMAGE_SECURITY_GUID,
    "dbx": _EFI_IMAGE_SECURITY_GUID,
}


def _efi_time_bytes():
    tm = time.gmtime(time.time() + 365 * 24 * 3600)
    return struct.pack(
        "<HBBBBBBIhBB",
        tm.tm_year + 1900,
        tm.tm_mon,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        0,
        0,
        0,
        0,
        0,
    )


def _esl_to_setup_auth(esl):
    ts = _efi_time_bytes()
    cert_type = uuid.UUID(_EFI_CERT_TYPE_PKCS7_GUID).bytes_le
    dw_length = 4 + 2 + 2 + 16
    win = struct.pack("<IHH", dw_length, 0x0200, _WIN_CERT_TYPE_EFI_GUID) + cert_type
    return ts + win + esl


def _write_setup_esl(var_name, esl_path, progress_callback=None):
    guid = _EFIVAR_GUID.get(var_name)
    if not guid:
        return False, f"unknown EFI variable {var_name}"
    try:
        with open(esl_path, "rb") as fh:
            esl = fh.read()
    except OSError as e:
        return False, str(e)
    if not esl:
        return False, f"empty ESL for {var_name}"
    blob = struct.pack("<I", _EFIVAR_NV_BS_RT_AT) + _esl_to_setup_auth(esl)
    tmp = tempfile.NamedTemporaryFile(prefix="centrio-efivar-", suffix=".bin", delete=False)
    try:
        tmp.write(blob)
        tmp.close()
        os.chmod(tmp.name, 0o644)
        dest = f"/sys/firmware/efi/efivars/{var_name}-{guid}"
        _run_command(["chattr", "-i", dest], f"chattr -i {var_name}", progress_callback, timeout=5)
        _run_command(["rm", "-f", dest], f"rm {var_name}", progress_callback, timeout=5)
        ok, err, _ = _run_command(
            ["dd", f"if={tmp.name}", f"of={dest}", "bs=65536"],
            f"Write {var_name} setup ESL",
            progress_callback,
            timeout=15,
        )
        if not ok:
            return False, err or f"failed to write {var_name}"
        return True, ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _efi_updatevar_esl(efi_updatevar, esl_path, var_name, progress_callback=None, append=False):
    cmd = [efi_updatevar]
    if append:
        cmd.append("-a")
    cmd.extend(["-e", "-f", esl_path, var_name])
    ok, err, _ = _run_command(
        cmd,
        f"efi-updatevar {'-a ' if append else ''}-e -f {os.path.basename(esl_path)} {var_name}",
        progress_callback,
        timeout=60,
    )
    if not ok:
        return False, err or f"efi-updatevar failed for {var_name}"
    return True, ""


def _snapshot_efivar_esl(var_name, out_path, progress_callback=None):
    guid = _EFIVAR_GUID.get(var_name)
    if not guid:
        return False
    src = f"/sys/firmware/efi/efivars/{var_name}-{guid}"
    raw_path = out_path + ".raw"
    ok, _, _ = _run_command(
        ["dd", f"if={src}", f"of={raw_path}", "bs=65536"],
        f"Snapshot {var_name}",
        progress_callback,
        timeout=15,
    )
    if not ok or not os.path.isfile(raw_path):
        return False
    _run_command(["chmod", "644", raw_path], "chmod snapshot", progress_callback, timeout=5)
    try:
        with open(raw_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    if len(raw) <= 4:
        return False
    with open(out_path, "wb") as fh:
        fh.write(raw[4:])
    return os.path.getsize(out_path) > 0


def _enroll_esl_list(efi_updatevar, var_name, esl_paths, progress_callback=None):
    dest = f"/sys/firmware/efi/efivars/{var_name}-{_EFIVAR_GUID[var_name]}"
    first = True
    for esl in esl_paths:
        _run_command(["chattr", "-i", dest], f"chattr -i {var_name}", progress_callback, timeout=5)
        ok, err = _efi_updatevar_esl(efi_updatevar, esl, var_name, progress_callback, append=not first)
        if not ok:
            if first:
                ok, err = _write_setup_esl(var_name, esl, progress_callback)
            if not ok:
                return False, err
        first = False
    return True, ""


def _enroll_setup_esl(efi_updatevar, esl_path, var_name, progress_callback=None):
    if var_name != "PK":
        ok, err = _efi_updatevar_esl(efi_updatevar, esl_path, var_name, progress_callback)
        if ok:
            return True, ""
        print(f"efi-updatevar -e {var_name} failed ({err}), writing AUTH2 ESL")
    return _write_setup_esl(var_name, esl_path, progress_callback)


def _efi_updatevar_binhash(efi_updatevar, efi_path, work, tag, progress_callback=None):
    staged = os.path.join(work, f"bhash-{tag}.efi")
    ok, err = _stage_efi_bin(efi_path, staged, progress_callback)
    if not ok:
        return False, err
    dest = f"/sys/firmware/efi/efivars/db-{_EFI_IMAGE_SECURITY_GUID}"
    _run_command(["chattr", "-i", dest], "chattr -i db", progress_callback, timeout=5)
    ok, err, _ = _run_command(
        [efi_updatevar, "-b", staged, "db"],
        f"efi-updatevar -b {os.path.basename(efi_path)} db",
        progress_callback,
        timeout=60,
    )
    if not ok:
        return False, err or f"efi-updatevar -b failed for {efi_path}"
    return True, ""


def _delete_secure_boot_vars(progress_callback=None):
    efivars = "/sys/firmware/efi/efivars"
    try:
        names = os.listdir(efivars)
    except OSError:
        return
    prefixes = ("db-", "KEK-", "PK-", "dbx-")
    for name in names:
        if not name.startswith(prefixes):
            continue
        path = os.path.join(efivars, name)
        _run_command(["chattr", "-i", path], f"chattr -i {name}", progress_callback, timeout=5)
        _run_command(["rm", "-f", path], f"rm {name}", progress_callback, timeout=5)


def _concat_files(paths, out_path):
    with open(out_path, "wb") as out:
        for p in paths:
            with open(p, "rb") as inp:
                out.write(inp.read())


def _split_pem_certs(text):
    certs = []
    buf = []
    inside = False
    for line in (text or "").splitlines(True):
        if "BEGIN CERTIFICATE" in line:
            inside = True
            buf = [line]
        elif inside:
            buf.append(line)
            if "END CERTIFICATE" in line:
                certs.append("".join(buf))
                inside = False
    return certs


def _read_asn1_len(data, off):
    if off >= len(data):
        return None
    b0 = data[off]
    if b0 < 0x80:
        return (1, b0)
    n = b0 & 0x7F
    if n < 1 or n > 4 or off + 1 + n > len(data):
        return None
    val = int.from_bytes(data[off + 1:off + 1 + n], "big")
    return (1 + n, val)


def _pe_cert_directory(data, opt, magic, size_opt):
    if magic == 0x20B:
        num_rva_off, dd_fixed = 108, 112
    elif magic == 0x10B:
        num_rva_off, dd_fixed = 92, 96
    else:
        return 0, 0
    if size_opt < dd_fixed + 40:
        return 0, 0
    num_rva = struct.unpack_from("<I", data, opt + num_rva_off)[0]
    starts = [opt + dd_fixed]
    if num_rva >= 5:
        alt = opt + size_opt - num_rva * 8
        if alt not in starts:
            starts.append(alt)
    for dd in starts:
        if dd + 40 > len(data):
            continue
        cert_off, cert_sz = struct.unpack_from("<II", data, dd + 32)
        if cert_off and cert_sz >= 8 and cert_off + 8 <= len(data):
            use_sz = min(cert_sz, len(data) - cert_off)
            if use_sz >= 8:
                return cert_off, use_sz
    return 0, 0


def _win_cert_blobs(data, cert_off, cert_sz):
    blobs = []
    pos = cert_off
    end = min(len(data), cert_off + cert_sz)
    while pos + 8 <= end:
        dw_length, _rev, wtype = struct.unpack_from("<IHH", data, pos)
        if dw_length < 8 or pos + dw_length > len(data):
            break
        payload = data[pos + 8:pos + dw_length]
        if wtype == _WIN_CERT_TYPE_EFI_GUID and len(payload) >= 16:
            payload = payload[16:]
        i = 0
        while i < len(payload) and payload[i] == 0:
            i += 1
        payload = payload[i:]
        if payload:
            blobs.append(payload)
            if payload[0] != 0x30 and len(payload) > 16:
                blobs.append(payload[16:])
        nxt = (dw_length + 7) & ~7
        if nxt < 8:
            break
        pos += nxt
    return blobs


def _pe_section_overlay_off(data, coff, opt, size_opt):
    nsec = struct.unpack_from("<H", data, coff + 2)[0]
    sec = opt + size_opt
    end = 0
    for i in range(nsec):
        off = sec + i * 40
        if off + 24 > len(data):
            break
        raw_size, raw_ptr = struct.unpack_from("<II", data, off + 16)
        if raw_ptr:
            end = max(end, raw_ptr + raw_size)
    if end and end + 8 <= len(data):
        return end
    soi = struct.unpack_from("<I", data, opt + 56)[0]
    if soi and soi + 8 <= len(data):
        return soi
    return 0


def _pe_pkcs7_blobs(data):
    if len(data) < 64 or data[:2] != b"MZ":
        return []
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return []
    coff = e_lfanew + 4
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    if opt + size_opt > len(data):
        return []
    magic = struct.unpack_from("<H", data, opt)[0]
    cert_off, cert_sz = _pe_cert_directory(data, opt, magic, size_opt)
    if cert_off:
        blobs = _win_cert_blobs(data, cert_off, cert_sz)
        if blobs:
            return blobs
    overlay = _pe_section_overlay_off(data, coff, opt, size_opt)
    if overlay:
        blobs = _win_cert_blobs(data, overlay, len(data) - overlay)
        if blobs:
            return blobs
    return []


def _pe_zero_cert_dir(data, opt, magic, size_opt):
    if magic == 0x20B:
        num_rva_off, dd_fixed = 108, 112
    elif magic == 0x10B:
        num_rva_off, dd_fixed = 92, 96
    else:
        return
    if size_opt < dd_fixed + 40:
        return
    num_rva = struct.unpack_from("<I", data, opt + num_rva_off)[0]
    starts = [opt + dd_fixed]
    if num_rva >= 5:
        alt = opt + size_opt - num_rva * 8
        if alt not in starts:
            starts.append(alt)
    for dd in starts:
        if dd + 40 > len(data):
            continue
        struct.pack_into("<II", data, dd + 32, 0, 0)


def _pe_strip_authenticode(data):
    if len(data) < 64 or data[:2] != b"MZ":
        return data
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return data
    coff = e_lfanew + 4
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    if opt + size_opt > len(data):
        return data
    magic = struct.unpack_from("<H", data, opt)[0]
    cert_off, cert_sz = _pe_cert_directory(data, opt, magic, size_opt)
    out = bytearray(data)
    _pe_zero_cert_dir(out, opt, magic, size_opt)
    cut = None
    overlay = _pe_section_overlay_off(bytes(out), coff, opt, size_opt)
    if cert_off and cert_off < len(out):
        if (overlay and cert_off >= overlay) or cert_off + cert_sz >= len(out) - 16:
            cut = cert_off
    if cut is None and overlay and overlay < len(out):
        if _win_cert_blobs(bytes(out), overlay, len(out) - overlay):
            cut = overlay
    if cut is not None:
        del out[cut:]
    return bytes(out)


def _write_efi_bytes(dest, data, progress_callback=None):
    tmp = tempfile.NamedTemporaryFile(prefix="centrio-efiout-", suffix=".efi", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        os.chmod(tmp.name, 0o644)
        ok, err, _ = _run_command(["cp", "-f", tmp.name, dest], f"Write {os.path.basename(dest)}", progress_callback, timeout=30)
        if not ok:
            return False, err or f"failed to write {dest}"
        return True, ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _strip_esp_authenticode(efi_path, work, tag, progress_callback=None):
    staged = os.path.join(work, f"strip-in-{tag}.efi")
    ok, err = _stage_efi_bin(efi_path, staged, progress_callback)
    if not ok:
        return False, err
    try:
        with open(staged, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return False, str(e)
    if not _pe_pkcs7_blobs(raw):
        return True, ""
    stripped = _pe_strip_authenticode(raw)
    if _pe_pkcs7_blobs(stripped):
        return False, f"failed to strip Authenticode from {efi_path}"
    ok, err = _write_efi_bytes(efi_path, stripped, progress_callback)
    if not ok:
        return False, err
    print(f"Stripped Authenticode from {efi_path}")
    return True, ""


def _x509_pems_from_der_blob(openssl, blob, work, prefix):
    pems = []
    i = 0
    n = len(blob)
    while i + 4 < n:
        if blob[i] != 0x30:
            i += 1
            continue
        parsed = _read_asn1_len(blob, i + 1)
        if not parsed:
            i += 1
            continue
        hdr, ln = parsed
        total = 1 + hdr + ln
        if ln < 256 or i + total > n:
            i += 1
            continue
        der_path = os.path.join(work, f"{prefix}-{i}.der")
        pem_path = os.path.join(work, f"{prefix}-{i}.pem")
        with open(der_path, "wb") as fh:
            fh.write(blob[i:i + total])
        ok, _ = _run_as_user(
            [openssl, "x509", "-inform", "DER", "-in", der_path, "-outform", "PEM", "-out", pem_path],
            f"x509 {os.path.basename(der_path)}",
            timeout=15,
        )
        if ok and os.path.isfile(pem_path) and os.path.getsize(pem_path) > 80:
            pems.append(pem_path)
            i += total
            continue
        i += 1
    return pems


def _cms_certsout(openssl, der_path, pem_path, work):
    dumped = os.path.join(work, os.path.basename(pem_path) + ".cmsout")
    for args in (
        [openssl, "cms", "-inform", "DER", "-in", der_path, "-verify", "-noverify", "-binary",
         "-certsout", pem_path, "-out", dumped],
        [openssl, "pkcs7", "-inform", "DER", "-in", der_path, "-print_certs", "-out", pem_path],
    ):
        ok, _ = _run_as_user(args, f"cms certs {os.path.basename(der_path)}", timeout=30)
        if ok and os.path.isfile(pem_path) and os.path.getsize(pem_path) > 80:
            return True
    return False


def _pesign_export_cer(efi_path, cer_path, progress_callback=None):
    pesign = shutil.which("pesign") or "/usr/bin/pesign"
    if not os.path.isfile(pesign):
        return False
    ok, _, _ = _run_command(
        [pesign, "-n", "0", "-i", efi_path, "-E", cer_path],
        f"pesign export {os.path.basename(efi_path)}",
        progress_callback,
        timeout=30,
    )
    return ok and os.path.isfile(cer_path) and os.path.getsize(cer_path) > 0


def _stage_efi_bin(src, dest, progress_callback=None):
    ok, err, _ = _run_command(["cp", "-a", src, dest], f"Stage {os.path.basename(src)}", progress_callback, timeout=30)
    if not ok or not os.path.isfile(dest):
        return False, err or f"failed to copy {src}"
    try:
        os.chmod(dest, 0o644)
    except OSError:
        _run_command(["chmod", "644", dest], "chmod staged EFI bin", progress_callback, timeout=5)
    return True, ""


def _pkg_efi_search_dirs(root):
    rels = [
        "usr/share/shim",
        "usr/share/shim/x64",
        "usr/lib/shim",
        "usr/lib64/shim",
        "usr/lib/shim/signed",
        "usr/lib/efi/BOOT",
    ]
    dirs = []
    for rel in rels:
        dirs.append(os.path.join(root, rel) if root else os.path.join("/", rel))
    return dirs


def _efi_score(path, has_auth):
    pl = path.replace("\\", "/")
    score = 0
    if has_auth:
        score += 1000
    if pl.endswith(".signed"):
        score += 500
    if "/EFI/fedora/" in pl or "/efi/EFI/fedora/" in pl:
        score -= 400
    if "/usr/lib/efi/" in pl:
        score -= 200
    if "/share/shim" in pl or "/lib/shim" in pl:
        score += 80
    if "/boot/efi/" in pl:
        score += 40
    return score


def _efi_has_auth(path, work, tag):
    staged = os.path.join(work, f"pick-{tag}.efi")
    ok, _ = _stage_efi_bin(path, staged)
    if not ok:
        return False
    try:
        with open(staged, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    return bool(_pe_pkcs7_blobs(data))


def _iter_named_efi(filename, target_root=None):
    seen = set()
    roots = []
    if target_root:
        roots.append(target_root)
    roots.append("")
    for root in roots:
        for d in _pkg_efi_search_dirs(root):
            for name in (filename, filename + ".signed"):
                p = os.path.join(d, name)
                if p not in seen and _efi_file_readable(p):
                    seen.add(p)
                    yield p
        efi_roots = []
        if root:
            efi_roots.append(os.path.join(root, "boot/efi/EFI"))
        else:
            efi_roots.extend(["/boot/efi/EFI", "/efi/EFI"])
        for efi in efi_roots:
            ok, _, text = _run_command(
                ["find", efi, "-maxdepth", "2", "(", "-name", filename, "-o", "-name", filename + ".signed", ")", "-type", "f"],
                f"find ESP {filename}",
                timeout=15,
            )
            if ok and text:
                for line in text.splitlines():
                    p = line.strip()
                    if p and p not in seen and _efi_file_readable(p):
                        seen.add(p)
                        yield p
        search = os.path.join(root, "usr") if root else "/usr"
        ok, _, text = _run_command(
            ["find", search, "-xdev", "(", "-name", filename, "-o", "-name", filename + ".signed", ")", "-type", "f"],
            f"find {filename}",
            timeout=40,
        )
        if ok and text:
            for line in text.splitlines():
                p = line.strip()
                if p and p not in seen and _efi_file_readable(p):
                    seen.add(p)
                    yield p


def _find_named_efi(filename, target_root=None):
    hits = list(_iter_named_efi(filename, target_root))
    if not hits:
        return None
    work = tempfile.mkdtemp(prefix="centrio-efi-pick-")
    try:
        best = None
        best_score = None
        for i, p in enumerate(hits):
            has_auth = _efi_has_auth(p, work, str(i))
            score = _efi_score(p, has_auth)
            if best_score is None or score > best_score:
                best_score = score
                best = p
        return best
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _hash_efi_to_esl(hash_tool, efi_path, esl_path, work, tag, progress_callback=None):
    staged = os.path.join(work, f"hash-{tag}.efi")
    ok, err = _stage_efi_bin(efi_path, staged, progress_callback)
    if not ok:
        return False, err
    ok, err = _run_as_user(
        [hash_tool, staged, esl_path],
        f"hash-to-efi-sig-list {os.path.basename(efi_path)}",
        progress_callback,
        timeout=30,
    )
    if not ok or not os.path.isfile(esl_path):
        return False, err or f"hash-to-efi-sig-list failed for {efi_path}"
    return True, ""


def _extract_pe_signer_pems(efi_path, work, idx, openssl, progress_callback=None):
    staged = os.path.join(work, f"pe-{idx}.efi")
    ok, err = _stage_efi_bin(efi_path, staged, progress_callback)
    if not ok:
        return [], err
    try:
        with open(staged, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return [], str(e)
    pems = []
    blobs = _pe_pkcs7_blobs(data)
    if not blobs:
        return [], f"{efi_path} has no Authenticode certificate table"
    for b_i, blob in enumerate(blobs):
        der = os.path.join(work, f"pe-{idx}-{b_i}.p7")
        pem = os.path.join(work, f"pe-{idx}-{b_i}.pem")
        with open(der, "wb") as fh:
            fh.write(blob)
        if _cms_certsout(openssl, der, pem, work):
            try:
                with open(pem, "r", encoding="ascii", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                text = ""
            for j, cert in enumerate(_split_pem_certs(text)):
                out = os.path.join(work, f"pe-{idx}-{b_i}-c{j}.pem")
                with open(out, "w", encoding="ascii") as fh:
                    fh.write(cert)
                pems.append(out)
        pems.extend(_x509_pems_from_der_blob(openssl, blob, work, f"pe-{idx}-{b_i}-x"))
    if not pems:
        cer = os.path.join(work, f"pe-{idx}.cer")
        if _pesign_export_cer(staged, cer, progress_callback):
            pem = os.path.join(work, f"pe-{idx}-pesign.pem")
            ok, _ = _cer_to_pem(cer, pem, progress_callback)
            if ok:
                pems.append(pem)
    if not pems:
        return [], f"no X.509 certs in Authenticode blob of {efi_path}"
    uniq = []
    seen = set()
    for p in pems:
        try:
            with open(p, "rb") as fh:
                key = fh.read()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq, ""


def _is_shim_path(path):
    return os.path.basename(path).lower().startswith("shim")


def _esp_loader_paths(target_root, efi_install_id):
    arch = get_host_architecture()
    vendor = os.path.join(target_root, "boot/efi/EFI", efi_install_id or BOOTLOADER_ID)
    boot = os.path.join(target_root, "boot/efi/EFI", "BOOT")
    paths = []
    for d, names in (
        (vendor, (arch["efi_shim"], arch["efi_grub"])),
        (boot, (arch["efi_boot"], arch["efi_grub"], arch["efi_shim"])),
    ):
        for name in names:
            p = os.path.join(d, name)
            if _efi_file_readable(p) and p not in paths:
                paths.append(p)
    return paths


def _sbctl_layout_cert(root=""):
    prefix = root if root else "/"
    rels = (
        "usr/share/secureboot/keys",
        "var/lib/sbctl/keys",
        "etc/sbctl/keys",
        "usr/share/oreon/secureboot/keys",
    )
    for rel in rels:
        base = os.path.join(prefix, rel) if root else os.path.join("/", rel)
        for sub in ("PK", "db", "KEK"):
            pem = os.path.join(base, sub, f"{sub}.pem")
            if _efi_file_readable(pem):
                return pem
    return None


def _oreon_sb_material(root=""):
    uki, kca = _find_kernel_sb_certs(root)
    if not kca:
        kca = _sbctl_layout_cert(root)
    return kca, uki


def provision_secure_boot_keys(target_root, progress_callback=None, efi_install_id=None):
    if not is_uefi_system():
        print("Skipping Secure Boot key enrollment (not UEFI).")
        return True, ""

    if progress_callback:
        progress_callback("Checking Secure Boot Setup Mode...", None)

    if not _is_secure_boot_setup_mode(progress_callback):
        print(
            "Secure Boot not in Setup Mode. Skipping efi-updatevar key enrollment."
        )
        return True, ""

    ca, uki = _oreon_sb_material("")
    if not ca:
        ca2, uki2 = _oreon_sb_material(target_root)
        ca = ca or ca2
        uki = uki or uki2
    if not ca:
        return False, "No kernel SB certs under /usr/share/doc/kernel-keys"

    efi_updatevar = _ensure_host_tool("efi-updatevar", target_root, progress_callback)
    cert_to_efi = _ensure_host_tool("cert-to-efi-sig-list", target_root, progress_callback)
    hash_tool = _ensure_host_tool("hash-to-efi-sig-list", target_root, progress_callback)
    if not efi_updatevar or not cert_to_efi:
        return False, "efitools not found (need efi-updatevar and cert-to-efi-sig-list)"
    if not hash_tool:
        return False, "efitools not found (need hash-to-efi-sig-list)"

    openssl = _openssl_bin()
    if not os.path.isfile(openssl):
        return False, "openssl not found"

    if progress_callback:
        progress_callback("Enrolling Oreon kernel Secure Boot certs...", None)

    work = tempfile.mkdtemp(prefix="centrio-sb-")
    try:
        kernel_pem = os.path.join(work, "kernel-ca.pem")
        ok, err = _cer_to_pem(ca, kernel_pem, progress_callback)
        if not ok:
            return False, err

        uki_pem = None
        if uki:
            uki_pem = os.path.join(work, "uki.pem")
            ok, err = _cer_to_pem(uki, uki_pem, progress_callback)
            if not ok:
                return False, err

        loaders = _esp_loader_paths(target_root, efi_install_id)
        if not loaders:
            return False, "No EFI loader on the ESP to enroll"

        arch = get_host_architecture()
        shim_esp = os.path.join(
            target_root, "boot/efi/EFI", efi_install_id or BOOTLOADER_ID, arch["efi_shim"]
        )
        if shim_esp not in loaders:
            loaders = [shim_esp] + loaders if _efi_file_readable(shim_esp) else loaders

        db_esls = []
        snap = os.path.join(work, "db-snapshot.esl")
        if _snapshot_efivar_esl("db", snap, progress_callback):
            db_esls.append(snap)
            print("Kept existing firmware db")
        kek_esls = []
        kek_snap = os.path.join(work, "kek-snapshot.esl")
        if _snapshot_efivar_esl("KEK", kek_snap, progress_callback):
            kek_esls.append(kek_snap)

        pk_esl = os.path.join(work, "pk.esl")
        ok, err = _cert_to_esl(cert_to_efi, kernel_pem, pk_esl, progress_callback)
        if not ok:
            return False, err
        db_esls.append(pk_esl)
        kek_esls.append(pk_esl)
        if uki_pem:
            esl = os.path.join(work, "uki.esl")
            ok, err = _cert_to_esl(cert_to_efi, uki_pem, esl, progress_callback)
            if not ok:
                return False, err
            db_esls.append(esl)

        pems, perr = _extract_pe_signer_pems(shim_esp, work, "shim", openssl, progress_callback)
        if not pems:
            print(f"No Authenticode certs in {shim_esp}: {perr}")
        for j, pem in enumerate(pems or []):
            esl = os.path.join(work, f"shim-pe-{j}.esl")
            ok, err = _cert_to_esl(cert_to_efi, pem, esl, progress_callback)
            if not ok:
                return False, err
            db_esls.append(esl)
            print(f"db cert from shim: {os.path.basename(pem)}")
        for i, src in enumerate(loaders):
            if os.path.realpath(src) == os.path.realpath(shim_esp):
                continue
            extra, _ = _extract_pe_signer_pems(src, work, f"ldr{i}", openssl, progress_callback)
            for j, pem in enumerate(extra or []):
                esl = os.path.join(work, f"ldr-{i}-{j}.esl")
                ok, err = _cert_to_esl(cert_to_efi, pem, esl, progress_callback)
                if ok:
                    db_esls.append(esl)
        for i, src in enumerate(loaders):
            esl = os.path.join(work, f"db-hash-{i}.esl")
            ok, herr = _hash_efi_to_esl(hash_tool, src, esl, work, str(i), progress_callback)
            if not ok:
                return False, herr
            db_esls.append(esl)

        _delete_secure_boot_vars(progress_callback)
        _clear_secure_boot_efivar_immutable(progress_callback)
        ok, err = _enroll_esl_list(efi_updatevar, "db", db_esls, progress_callback)
        if not ok:
            return False, err or "failed to enroll db"
        print("Enrolled db")
        ok, err = _enroll_esl_list(efi_updatevar, "KEK", kek_esls, progress_callback)
        if not ok:
            return False, err or "failed to enroll KEK"
        print("Enrolled KEK")
        for i, src in enumerate(loaders):
            ok, err = _efi_updatevar_binhash(efi_updatevar, src, work, str(i), progress_callback)
            if not ok:
                print(f"efi-updatevar -b skipped for {src}: {err}")
        ok, err = _write_setup_esl("PK", pk_esl, progress_callback)
        if not ok:
            return False, err
        print("Enrolled PK from kernel-keys")

        if _is_secure_boot_setup_mode(progress_callback):
            return False, "PK enroll did not leave Setup Mode"
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("Oreon Secure Boot key enrollment completed.")
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


def _find_shim_grub(target_root=None):
    arch = get_host_architecture()
    pkg_shim = _find_named_efi(arch["efi_shim"], target_root)
    pkg_grub = _find_named_efi(arch["efi_grub"], target_root)
    if pkg_shim and pkg_grub:
        return pkg_shim, pkg_grub, BOOTLOADER_ID
    return _find_shim_grub_on_host()


def _find_shim_grub_on_host():
    arch = get_host_architecture()
    efi_shim = arch["efi_shim"]
    efi_grub = arch["efi_grub"]
    efi_boot = arch["efi_boot"]
    pkg_shim = _find_named_efi(efi_shim, None)
    pkg_grub = _find_named_efi(efi_grub, None)
    if pkg_shim and pkg_grub:
        return pkg_shim, pkg_grub, BOOTLOADER_ID
    if pkg_shim:
        pkg_grub = _find_named_efi(efi_grub, None) or _find_named_efi("grubx64.efi", None)
        if pkg_grub:
            return pkg_shim, pkg_grub, BOOTLOADER_ID
    vendors = ["oreon", "fedora", "centos", "rhel", "rocky", "almalinux"]
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


def _install_efi_boot_fallback(tmp_mount, shim_src, grub_src, arch, progress_callback=None):
    efi_boot = os.path.join(tmp_mount, "EFI", "BOOT")
    if not _ensure_directory(efi_boot, progress_callback):
        return False, "Failed to create EFI/BOOT on ESP"
    dest = os.path.join(efi_boot, arch["efi_boot"])
    bak = dest + ".windows.bak"
    if _efi_file_readable(dest) and not _efi_file_readable(bak):
        ok, err, _ = _run_command(["cp", dest, bak], "Backup existing EFI/BOOT fallback", progress_callback)
        if not ok:
            print(f"Warning: could not backup EFI/BOOT fallback: {err}")
    ok, err, _ = _run_command(["cp", shim_src, dest], "Copy shim to EFI/BOOT", progress_callback)
    if not ok:
        return False, err or "Failed to copy shim to EFI/BOOT"
    ok, err, _ = _run_command(
        ["cp", grub_src, os.path.join(efi_boot, arch["efi_grub"])],
        "Copy grub to EFI/BOOT",
        progress_callback,
    )
    if not ok:
        return False, err or "Failed to copy grub to EFI/BOOT"
    return True, ""


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
    Mounts the target ESP to a private temp dir so we always write to the correct partition."""
    if not efi_partition_device:
        return False, "UEFI install requires the EFI partition device (e.g. /dev/sda1).", None
    if not os.path.exists(efi_partition_device):
        return False, "EFI partition device does not exist: %s" % efi_partition_device, None

    from backend import verify_grub_packages
    vok, verr, _ = verify_grub_packages(target_root, offline_install=offline_install)
    if not vok:
        return False, verr or "Required GRUB packages missing.", None

    shim_src, grub_src, efi_vendor = _find_shim_grub(target_root)
    if not shim_src or not grub_src:
        return False, "No signed shim/grub found in packages or EFI.", None

    arch = get_host_architecture()
    efi_install_id = BOOTLOADER_ID
    tmp_mount = tempfile.mkdtemp(prefix="centrio_efi_")
    try:
        ok, err, _ = _run_command(
            ["mount", efi_partition_device, tmp_mount],
            "Mount ESP at temp dir", progress_callback, timeout=30
        )
        if not ok:
            return False, err or "Failed to mount ESP at temp dir", None

        efi_dir = os.path.join(tmp_mount, "EFI", efi_install_id)
        if not _ensure_directory(efi_dir, progress_callback):
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
            return False, "Failed to create EFI dirs on ESP", None

        for s, d in (
            (shim_src, os.path.join(efi_dir, arch["efi_shim"])),
            (grub_src, os.path.join(efi_dir, arch["efi_grub"])),
        ):
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

        ok, err = _install_efi_boot_fallback(tmp_mount, shim_src, grub_src, arch, progress_callback)
        if not ok:
            _run_command(["umount", tmp_mount], "Unmount ESP", progress_callback, timeout=15)
            return False, err, None

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
    bootmgfw = os.path.join(
        target_root, "boot", "efi", "EFI", "Microsoft", "Boot", "bootmgfw.efi"
    )
    if not _efi_file_readable(bootmgfw):
        print("No Windows bootmgfw.efi on target ESP, skip Windows GRUB entry")
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
    If grub2-mkconfig produces empty/small output, falls back to copying grub.cfg from the live env and patching root UUID."""
    grub_cfg_chroot = "/boot/grub2/grub.cfg"
    cfg_path = os.path.join(target_root, "boot", "grub2", "grub.cfg")

    if dual_boot:
        _patch_grub_default_os_prober(target_root, enable=True, progress_callback=progress_callback)
        if is_uefi:
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
    Install bootloader for target: UEFI or legacy BIOS.
    On UEFI in Setup Mode, enrolls kernel SB certs via efi-updatevar.
    Returns (success, error_msg, verification_dict or None).
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

    if uefi and efi_partition_device:
        _efi_partition_ensure_mounted(target_root, efi_partition_device, progress_callback)

    ok, err = _generate_grub_cfg(
        target_root, primary_disk, uefi, progress_callback, dual_boot=dual_boot
    )
    if not ok:
        return False, err, None

    if uefi:
        if progress_callback:
            progress_callback("Enrolling Secure Boot keys (efi-updatevar)...", None)
        ok_sb, err_sb = provision_secure_boot_keys(
            target_root, progress_callback=progress_callback, efi_install_id=efi_install_id
        )
        if not ok_sb:
            return False, err_sb or "Secure Boot key enrollment failed", None

    verification = {
        "uefi": uefi,
        "bootloader_id": efi_install_id if uefi else BOOTLOADER_ID,
        "primary_disk": primary_disk,
        "efi_partition": efi_partition_device if uefi else None,
        "dual_boot": dual_boot,
    }
    return True, "", verification
