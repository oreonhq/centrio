import os
import re

LSBLK_COLUMNS = "NAME,PATH,SIZE,MODEL,VENDOR,TYPE,PKNAME,MOUNTPOINT,TRAN,RO,RM,HOTPLUG"

SKIP_NAME_PREFIXES = ("zram", "loop", "ram", "sr", "fd", "nbd", "dm-", "md", "drbd", "mtdblock")
SKIP_TYPES = ("rom", "loop", "lvm", "crypt", "dm", "mpath", "part")
PORTABLE_TRANSPORTS = ("usb", "ieee1394", "firewire", "memstick")
TRANSPORT_LABELS = {
    "nvme": "NVMe disk",
    "sata": "SATA disk",
    "ata": "ATA disk",
    "sas": "SAS disk",
    "spi": "SCSI disk",
    "fc": "Fibre Channel disk",
    "iscsi": "iSCSI disk",
    "mmc": "MMC storage",
    "usb": "USB disk",
    "virtio": "Virtual disk",
}


def _flag(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _size(dev):
    try:
        return int(dev.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _base_name(dev):
    name = str(dev.get("name") or dev.get("path") or "").strip()
    return os.path.basename(name)


def _is_md_member(dev):
    for child in dev.get("children") or []:
        ctype = str(child.get("type") or "").lower()
        if ctype.startswith("raid") or ctype == "md":
            return True
        if _is_md_member(child):
            return True
    return False


def is_installable_disk(dev, live_medium=()):
    name = _base_name(dev)
    path = str(dev.get("path") or "").strip()
    if not name or not path.startswith("/dev/"):
        return False
    if str(dev.get("type") or "").strip().lower() != "disk":
        return False
    if name.startswith(SKIP_NAME_PREFIXES):
        return False
    if _flag(dev.get("ro")):
        return False
    if _size(dev) <= 0:
        return False
    if any(s in str(dev.get("model") or "").upper() for s in ("CD-ROM", "DVD", "CDROM")):
        return False
    if _is_md_member(dev):
        return False
    if live_medium and os.path.realpath(path) in live_medium:
        return False
    return True


def is_portable_disk(dev):
    if _flag(dev.get("rm")):
        return True
    tran = str(dev.get("tran") or "").strip().lower()
    if tran in PORTABLE_TRANSPORTS:
        return True
    name = _base_name(dev)
    if name.startswith("mmcblk"):
        try:
            with open(f"/sys/block/{name}/device/type", encoding="utf-8") as fh:
                return fh.read().strip() == "SD"
        except OSError:
            return False
    return False


def display_model(dev):
    model = " ".join(str(dev.get("model") or "").split())
    if model:
        return model
    vendor = " ".join(str(dev.get("vendor") or "").split())
    if vendor and not vendor.lower().startswith("0x"):
        return f"{vendor} disk"
    tran = str(dev.get("tran") or "").strip().lower()
    name = _base_name(dev)
    if name.startswith(("vd", "xvd")):
        return "Virtual disk"
    if name.startswith("nvme"):
        return "NVMe disk"
    if name.startswith("mmcblk"):
        return "MMC storage"
    if tran in TRANSPORT_LABELS:
        return TRANSPORT_LABELS[tran]
    if tran:
        return f"{tran.upper()} disk"
    return "Disk"


def _natural_key(name):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name or "")]


def installable_disks(blockdevices, live_medium=()):
    disks = []
    for dev in blockdevices or []:
        if not is_installable_disk(dev, live_medium):
            continue
        disks.append({
            "name": _base_name(dev),
            "path": str(dev.get("path")).strip(),
            "size": _size(dev),
            "model": display_model(dev),
            "tran": str(dev.get("tran") or "").strip().lower(),
            "portable": is_portable_disk(dev),
        })
    return disks


def default_disk(disks, unusable=()):
    internal = [
        d for d in disks
        if not d.get("portable") and d.get("path") not in unusable and not d.get("is_live_os_disk")
    ]
    if not internal:
        return None
    internal.sort(key=lambda d: _natural_key(d.get("name") or d.get("path")))
    return internal[0].get("path")
