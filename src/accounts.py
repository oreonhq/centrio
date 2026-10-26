import re
import subprocess

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
MAX_USERNAME_LEN = 32
RESERVED_USERNAMES = frozenset({
    "root", "bin", "daemon", "adm", "lp", "sync", "shutdown", "halt", "mail",
    "operator", "games", "ftp", "nobody", "systemd-network", "systemd-resolve",
    "systemd-timesync", "systemd-coredump", "systemd-oom", "dbus", "polkitd",
    "sshd", "chrony", "tss", "rpc", "rpcuser", "nfsnobody", "avahi", "colord",
    "geoclue", "gdm", "sddm", "pipewire", "rtkit", "setroubleshoot", "saslauth",
    "unbound", "dnsmasq", "qemu", "libstoragemgmt", "clevis", "flatpak",
    "gnome-initial-setup", "gnome-remote-desktop", "usbmuxd", "cockpit-ws",
    "cockpit-wsinstance", "wheel", "users", "admin", "sudo", "liveuser", "live",
    "fedora", "gnome", "tcpdump", "abrt", "apache", "nginx", "mysql", "postgres",
})


def username_error(username):
    if not username:
        return "Enter a username"
    if len(username) > MAX_USERNAME_LEN:
        return f"Username must be at most {MAX_USERNAME_LEN} characters"
    if not USERNAME_RE.match(username):
        return "Use lowercase letters, digits, - or _ and start with a letter or _"
    if username in RESERVED_USERNAMES:
        return f"{username} is reserved by the system"
    return ""


def password_error(password, confirm):
    if not password:
        return "Enter a password"
    if password != confirm:
        return "Passwords do not match"
    return ""


def validate_accounts(create_user, username, user_password, user_confirm, enable_root, root_password, root_confirm):
    errors = {}
    if not create_user and not enable_root:
        errors["general"] = "Create a user account, enable root, or both"
    if create_user:
        err = username_error(username)
        if err:
            errors["username"] = err
        err = password_error(user_password, user_confirm)
        if err:
            errors["user_password"] = err
    if enable_root:
        err = password_error(root_password, root_confirm)
        if err:
            errors["root_password"] = err
    return errors


def hash_password(password):
    try:
        r = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        hashed = (r.stdout or "").strip()
        if r.returncode == 0 and hashed.startswith("$6$"):
            return hashed
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        import crypt
        hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
        if hashed and hashed.startswith("$6$"):
            return hashed
    except Exception:
        pass
    raise RuntimeError("Could not hash the password")


def build_accounts_config(create_user, real_name, username, user_password, is_admin, enable_root, root_password):
    config = {"user": None, "root": None}
    if create_user:
        config["user"] = {
            "username": username,
            "real_name": real_name,
            "is_admin": bool(is_admin),
            "password_hash": hash_password(user_password),
        }
    if enable_root:
        config["root"] = {"password_hash": hash_password(root_password)}
    config["root_ssh_password_login"] = bool(enable_root and not create_user)
    return config
