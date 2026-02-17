# centrio_installer/backend.py

import subprocess
import shlex
import os
import re # For parsing os-release
from utils import get_os_release_info
import errno # For checking mount errors
import time   # For delays
import shutil # For copying bootloader files

def _run_command(command_list, description, progress_callback=None, timeout=None, pipe_input=None):
    """Runs a command, using pkexec if not already root, captures output, handles errors.
    
    Checks os.geteuid() to determine if running as root.
    """
    
    is_root = os.geteuid() == 0
    final_command_list = []
    execution_method = ""

    if is_root:
        final_command_list = command_list
        execution_method = "directly as root"
        print(f"Executing Backend Step ({execution_method}): {description} -> {' '.join(shlex.quote(c) for c in final_command_list)}")
    else:
        # Prepend pkexec if not running as root
        final_command_list = ["pkexec"] + command_list
        execution_method = "via pkexec"
        cmd_str = ' '.join(shlex.quote(c) for c in final_command_list)
        print(f"Executing Backend Step ({execution_method}): {description} -> {cmd_str}")
        if progress_callback:
            progress_callback(f"Requesting privileges for: {description}...")
        
    stderr_output = ""
    stdout_output = ""
    try:
        # Run the command (either directly or with pkexec)
        process = subprocess.Popen(
            final_command_list, # Use the decided command list
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if pipe_input is not None else None,
            text=True
        )
        
        stdout_output, stderr_output = process.communicate(input=pipe_input, timeout=timeout)
        
        print(f"  Command {description} stdout:\n{stdout_output.strip()}")
        if stderr_output:
             # Filter pkexec messages only if running via pkexec
             filtered_stderr = stderr_output
             if execution_method == "via pkexec":
                  filtered_stderr = "\n".join(line for line in stderr_output.splitlines() if "using backend" not in line)
             
             if filtered_stderr.strip():
                 print(f"  Command {description} stderr:\n{filtered_stderr.strip()}")

        if process.returncode != 0:
            error_detail = stderr_output.strip() or f"Exited with code {process.returncode}"
            # Check for pkexec/PolicyKit errors only if running via pkexec
            error_msg = f"{description} failed ({execution_method}): {error_detail}"
            if execution_method == "via pkexec":
                if "Authentication failed" in error_detail or process.returncode == 127:
                     error_msg = f"Authorization failed for {description}. Check PolicyKit rules or password."
                elif "Cannot run program" in error_detail or process.returncode == 126:
                     error_msg = f"Command not found or not permitted by PolicyKit for {description}: {command_list[0]}"
                # else: use the generic error_msg already set
            
            print(f"ERROR: {error_msg}")
            
            # --- Add dmesg logging on error --- 
            print("--- Attempting to get last kernel messages (dmesg) ---")
            try:
                 # Run dmesg directly, not via _run_command to avoid loops/pkexec issues
                 dmesg_cmd = ["dmesg", "-T"] 
                 dmesg_process = subprocess.run(dmesg_cmd, capture_output=True, text=True, check=False, timeout=5)
                 if dmesg_process.stdout:
                      # Use tail command for potentially large output (more reliable than split/slice)
                      tail_process = subprocess.Popen(["tail", "-n", "50"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                      dmesg_tail_stdout, _ = tail_process.communicate(input=dmesg_process.stdout)
                      print(f"Last 50 lines of dmesg:\n{dmesg_tail_stdout.strip()}")
                 else:
                      print("Could not capture dmesg output.")
                 if dmesg_process.stderr:
                      print(f"dmesg stderr: {dmesg_process.stderr.strip()}")
            except FileNotFoundError:
                 print("dmesg or tail command not found.")
            except Exception as dmesg_e:
                 print(f"Failed to run or capture dmesg: {dmesg_e}")
            print("-----------------------------------------------------")
            # --- End dmesg logging --- 
            
            return False, error_msg, stdout_output.strip() 
            
        print(f"SUCCESS: {description} completed ({execution_method}).")
        return True, "", stdout_output.strip()

    except FileNotFoundError:
        # Handle command-not-found specifically
        cmd_not_found = final_command_list[0]
        err = f"Command not found: {cmd_not_found}. Ensure it's installed and in the PATH."
        if execution_method == "via pkexec" and cmd_not_found == "pkexec":
            err = "Command not found: pkexec. Cannot run privileged commands."
        print(f"ERROR: {err}")
        return False, err, None 
    except subprocess.TimeoutExpired:
        err = f"Timeout expired after {timeout}s for {description} ({execution_method})."
        try:
            process.kill()
            process.wait()
        except Exception as kill_e:
            print(f"Warning: Error trying to kill timed out process: {kill_e}")
        return False, err, stdout_output.strip() 
    except Exception as e:
        err_detail = stderr_output.strip() or str(e)
        err = f"Unexpected error during {description} ({execution_method}): {err_detail}"
        print(f"ERROR: {err}")
        return False, err, stdout_output.strip()

# --- New _run_in_chroot function ---
def _run_in_chroot(target_root, command_list, description, progress_callback=None, timeout=None, pipe_input=None):
    """Runs a command inside the target root using chroot, managing bind mounts.
    
    Requires manual mounting/unmounting of /proc, /sys, /dev, /dev/pts, and /etc/resolv.conf.
    Assumes the caller (_run_command) handles root privileges.
    """
    host_dbus_socket = "/run/dbus/system_bus_socket"
    target_dbus_socket = os.path.join(target_root, host_dbus_socket.lstrip('/'))
    
    mount_points = {
        "proc": os.path.join(target_root, "proc"),
        "sys": os.path.join(target_root, "sys"),
        "dev": os.path.join(target_root, "dev"),
        "dev/pts": os.path.join(target_root, "dev/pts"),
        "resolv.conf": os.path.join(target_root, "etc/resolv.conf"),
        "dbus": target_dbus_socket # Add dbus socket target
    }
    mounted_paths = []  # Changed to list to maintain order and store (target, name) tuples
    
    # Add efivars path if host supports EFI
    host_efi_vars_path = "/sys/firmware/efi/efivars"
    if os.path.exists(host_efi_vars_path):
        mount_points["efivars"] = os.path.join(target_root, host_efi_vars_path.lstrip('/'))
        
    # Add /boot path if it exists within target_root
    target_boot_path = os.path.join(target_root, "boot")
    if os.path.exists(target_boot_path):
        mount_points["boot"] = target_boot_path # Target is the same as source for bind mount
        
    # Add /boot/efi path if it exists and is mounted
    target_boot_efi_path = os.path.join(target_root, "boot/efi")
    if os.path.exists(target_boot_efi_path):
        # Check if it's mounted by looking for any mount activity
        try:
            # Use findmnt to check if this is a mount point
            findmnt_cmd = ["findmnt", target_boot_efi_path]
            findmnt_result = subprocess.run(findmnt_cmd, capture_output=True, text=True, check=False, timeout=5)
            if findmnt_result.returncode == 0:
                mount_points["boot_efi"] = target_boot_efi_path
                print(f"  Will bind-mount /boot/efi into chroot: {target_boot_efi_path}")
            else:
                print(f"  /boot/efi exists but is not mounted: {target_boot_efi_path}")
        except Exception as e:
            print(f"  Warning: Could not check /boot/efi mount status: {e}")
            # If we can't check, but the directory exists, try to include it anyway
            if os.path.exists(target_boot_efi_path):
                mount_points["boot_efi"] = target_boot_efi_path
                print(f"  Including /boot/efi in chroot anyway: {target_boot_efi_path}")
    else:
        print(f"  /boot/efi directory does not exist: {target_boot_efi_path}")
    
    try:
        # --- Mount API filesystems, resolv.conf, and D-Bus socket --- 
        print(f"Setting up chroot environment in {target_root}...")
        
        # Prepare target directories/files first
        resolv_conf_target = mount_points["resolv.conf"]
        resolv_conf_dir = os.path.dirname(resolv_conf_target)
        
        # Ensure target /etc directory exists (still needed for potential D-Bus dir below)
        if not os.path.exists(resolv_conf_dir):
             try:
                 print(f"  Creating directory {resolv_conf_dir}...")
                 os.makedirs(resolv_conf_dir, exist_ok=True)
             except OSError as e:
                 raise RuntimeError(f"Failed to create target directory {resolv_conf_dir}: {e}") from e
                 
        # Ensure target /etc/resolv.conf file exists for bind mount
        # --- Block MODIFIED TO DO NOTHING --- 
        if not os.path.exists(resolv_conf_target):
            # Try block is now empty
            try:
                pass # Do nothing, file should be copied by progress.py
            except OSError as e:
                 # This should now be unreachable
                 raise RuntimeError(f"Failed to create target file {resolv_conf_target}: {e}") from e
        # --- End Block MODIFIED --- 
                 
        if os.path.exists(host_dbus_socket):
             dbus_target_dir = os.path.dirname(mount_points["dbus"])
             try:
                 os.makedirs(dbus_target_dir, exist_ok=True)
                 # Create an empty file for the socket bind mount target?
                 # Or maybe just mount the socket file directly? Mount requires dir for source/target usually?
                 # Let's try mounting the socket file directly using --bind.
             except OSError as e:
                 raise RuntimeError(f"Failed to prepare target D-Bus directory {dbus_target_dir}: {e}") from e
        else:
             print(f"Warning: Host D-Bus socket {host_dbus_socket} not found. Services inside chroot might fail.")

        # Refactored structure: (name, source, target, fstype, options_list)
        mount_commands = [
            ("proc",    "proc",                mount_points["proc"],        "proc",    ["nodev","noexec","nosuid"]), 
            ("sysfs",   "sys",                 mount_points["sys"],         "sysfs",   ["nodev","noexec","nosuid"]), 
            ("devtmpfs","udev",               mount_points["dev"],         "devtmpfs",["mode=0755","nosuid"]), 
            ("devpts",  "devpts",              mount_points["dev/pts"],     "devpts",  ["mode=0620","gid=5","nosuid","noexec"]), 
            ("bind",    host_dbus_socket,      mount_points["dbus"],        None,      ["--bind"]),
            # Conditionally add efivars mount
            ("efivars", "efivarfs",            mount_points.get("efivars"), "efivarfs",["nosuid","noexec","nodev"]), # Source is the fstype
            ("boot",    target_boot_path,      mount_points.get("boot"),      None,      ["--bind"]),
            ("boot_efi", target_boot_efi_path, mount_points.get("boot_efi"),  None,      ["--bind"])
        ]

        for name, source, target, fstype, options_list in mount_commands:
            # Skip D-Bus mount if source doesn't exist
            if name == "bind" and source == host_dbus_socket and not os.path.exists(host_dbus_socket):
                 print(f"  Skipping D-Bus socket mount (source {host_dbus_socket} not found).")
                 continue
                 
            # Skip efivars mount if target wasn't added (host doesn't have it)
            if name == "efivars" and not target:
                 print(f"  Skipping efivars mount (host path {host_efi_vars_path} not found).")
                 continue
                 
            # Skip boot mount if target wasn't added
            if name == "boot" and not target:
                 print(f"  Skipping boot mount (directory {target_boot_path} not found).")
                 continue
                 
            # Skip boot_efi mount if target wasn't added (not mounted)
            if name == "boot_efi" and not target:
                 print(f"  Skipping boot_efi mount (EFI partition not mounted or directory not found).")
                 continue
                 
            try:
                # Ensure target dir exists for non-file bind mounts
                if name != "bind" or source == "/etc/resolv.conf": # resolv.conf needs dir
                     os.makedirs(target, exist_ok=True)
                # For the dbus socket bind mount
                elif name == "bind" and source == host_dbus_socket:
                     os.makedirs(os.path.dirname(target), exist_ok=True)
                     # Create empty file as mount target if it doesn't exist? Bind mount needs a target.
                     if not os.path.exists(target):
                         open(target, 'a').close() 
                          
                # Construct mount command correctly
                mount_cmd = ["mount"]
                
                # --- Special Handling for resolv.conf bind mount ---
                # If target file exists, remove it first, as mount --bind might require it.
                # if name == "bind" and source == "/etc/resolv.conf":
                #     if os.path.exists(target):
                #         print(f"  Target file {target} exists. Removing before bind mount.")
                #         try:
                #             os.remove(target)
                #         except OSError as rm_e:
                #             print(f"  Warning: Failed to remove existing {target}: {rm_e}")
                #             # Continue anyway, maybe mount will still work or overwrite?
                # --------------------------------------------------
                
                if fstype:
                    mount_cmd.extend(["-t", fstype])
                
                # Handle options - differentiate between --bind and -o list
                if "--bind" in options_list:
                    mount_cmd.append("--bind")
                elif options_list: # Only add -o if there are other options
                    mount_cmd.extend(["-o", ",".join(options_list)])
                    
                mount_cmd.extend([source, target])
                
                print(f"  Mounting {source} -> {target} ({name}) with command: {' '.join(shlex.quote(c) for c in mount_cmd)}")
                result = subprocess.run(mount_cmd, check=True, capture_output=True, text=True, timeout=15)
                mounted_paths.append((target, name))
            except FileNotFoundError:
                 raise RuntimeError("Mount command failed: 'mount' executable not found.")
            except subprocess.CalledProcessError as e:
                # Check if already mounted (exit code 32 often means this)
                if e.returncode == 32 and ("already mounted" in e.stderr or "mount point does not exist" in e.stderr or "Not a directory" in e.stderr): # Added check for dbus socket
                    print(f"    Warning: Mount for {target} possibly already exists or target invalid? {e.stderr.strip()}")
                    mounted_paths.append((target, name)) 
                else:
                    raise RuntimeError(f"Failed to mount {source} to {target}: {e.stderr.strip()}") from e
            except Exception as e:
                 raise RuntimeError(f"Unexpected error mounting {source}: {e}") from e

        # --- Execute command in chroot --- 
        chroot_cmd = ["chroot", target_root] + command_list
        # Use _run_command to handle execution (it checks root/pkexec itself)
        success, err, stdout = _run_command(chroot_cmd, description, progress_callback, timeout, pipe_input)
        return success, err, stdout
        
    finally:
        # --- Unmount in reverse order ---
        try:
            print("Cleaning up chroot environment...")
            for mount_info in reversed(mounted_paths):
                 mount_target, mount_name = mount_info
                 
                 # Skip unmounting /boot/efi if we're in the middle of installation
                 # It should remain mounted for bootloader installation
                 if mount_name == "boot_efi":
                     print(f"  Preserving EFI mount for bootloader installation: {mount_target}")
                     continue
                 
                 try:
                     print(f"  Unmounting {mount_target}...")
                     umount_cmd = ["umount", mount_target]
                     result = subprocess.run(umount_cmd, capture_output=True, text=True, check=True, timeout=30)
                     print(f"    Successfully unmounted {mount_target}")
                 except subprocess.CalledProcessError as e:
                     print(f"    Warning: Failed to unmount {mount_target}: {e.stderr.strip()}")
                     # Try lazy unmount as fallback
                     try:
                         lazy_umount_cmd = ["umount", "-l", mount_target]
                         subprocess.run(lazy_umount_cmd, capture_output=True, text=True, check=True, timeout=15)
                         print(f"    Lazy unmount successful for {mount_target}")
                     except Exception as lazy_e:
                         print(f"    Warning: Lazy unmount also failed for {mount_target}: {lazy_e}")
                 except Exception as e:
                     print(f"    Warning: Error unmounting {mount_target}: {e}")
        except Exception as e:
            print(f"Warning: Error during chroot cleanup: {e}")

# --- Configuration Functions ---

def configure_system_in_container(target_root, config_data, progress_callback=None):
    """Configures timezone, locale, keyboard, hostname in target via chroot.
    Modified to write directly to config files instead of using systemd tools.
    """
    all_success = True
    errors = []
    
    # --- Timezone --- 
    tz = config_data.get('timedate', {}).get('timezone')
    if tz:
        print(f"Configuring Timezone to {tz}...")
        tz_file_path = os.path.join(target_root, "etc/timezone")
        localtime_path = os.path.join(target_root, "etc/localtime")
        zoneinfo_path = os.path.join(target_root, f"usr/share/zoneinfo/{tz}")
        
        try:
            # Write timezone name to /etc/timezone
            print(f"  Writing timezone name to {tz_file_path}...")
            with open(tz_file_path, 'w') as f:
                f.write(f"{tz}\n")
            
            # Link /etc/localtime to zoneinfo file
            if os.path.exists(zoneinfo_path):
                print(f"  Linking {localtime_path} -> {zoneinfo_path}...")
                # Remove existing link/file first if it exists
                if os.path.lexists(localtime_path):
                    os.remove(localtime_path)
                os.symlink(f"/usr/share/zoneinfo/{tz}", localtime_path) # Link relative to root
            else:
                print(f"  Warning: Zoneinfo file not found at {zoneinfo_path}. Cannot link /etc/localtime.")
                # Don't mark as failure, system might cope or use /etc/timezone
                
        except Exception as e:
            err_msg = f"Failed to configure timezone {tz}: {e}"
            print(f"  ERROR: {err_msg}")
            errors.append(err_msg)
            all_success = False
    else:
        print("Skipping timezone configuration (not provided).")

    # --- Locale --- 
    locale = config_data.get('language', {}).get('locale')
    if locale:
        print(f"Configuring Locale to {locale}...")
        locale_conf_path = os.path.join(target_root, "etc/locale.conf")
        try:
            print(f"  Writing locale to {locale_conf_path}...")
            with open(locale_conf_path, 'w') as f:
                f.write(f"LANG={locale}\n")
        except Exception as e:
            err_msg = f"Failed to configure locale {locale}: {e}"
            print(f"  ERROR: {err_msg}")
            errors.append(err_msg)
            all_success = False
    else:
         print("Skipping locale configuration (not provided).")

    # --- Keymap --- 
    keymap = config_data.get('keyboard', {}).get('layout')
    if keymap:
        print(f"Configuring Keymap to {keymap}...")
        vconsole_conf_path = os.path.join(target_root, "etc/vconsole.conf")
        try:
            print(f"  Writing keymap to {vconsole_conf_path}...")
            with open(vconsole_conf_path, 'w') as f:
                f.write(f"KEYMAP={keymap}\n")
        except Exception as e:
            err_msg = f"Failed to configure keymap {keymap}: {e}"
            print(f"  ERROR: {err_msg}")
            errors.append(err_msg)
            all_success = False
    else:
        print("Skipping keymap configuration (not provided).")
        
    # --- Hostname --- 
    hostname = config_data.get('network', {}).get('hostname')
    if hostname:
        print(f"Configuring Hostname to {hostname}...")
        hostname_path = os.path.join(target_root, "etc/hostname")
        try:
            print(f"  Writing hostname to {hostname_path}...")
            with open(hostname_path, 'w') as f:
                f.write(f"{hostname}\n")
        except Exception as e:
            err_msg = f"Failed to configure hostname {hostname}: {e}"
            print(f"  ERROR: {err_msg}")
            errors.append(err_msg)
            all_success = False
    else:
        print("Skipping hostname configuration (not provided).")

    final_error_str = "\n".join(errors)
    return all_success, final_error_str

def create_user_in_container(target_root, user_config, progress_callback=None):
    """Creates user account in target via chroot."""
    username = user_config.get('username')
    password = user_config.get('password', None) # Get password from config
    is_admin = user_config.get('is_admin', False)
    real_name = user_config.get('real_name', '') 
    
    if not username:
        return False, "Username not provided in user configuration.", None
    # Allow proceeding even if password is None or empty, chpasswd might handle it or fail later
    # if not password:
    #      return False, "Password not provided for user creation.", None

    # Build useradd command
    useradd_cmd = ["useradd", "-m", "-s", "/bin/bash", "-U"]
    if real_name:
        useradd_cmd.extend(["-c", real_name])
    if is_admin:
        useradd_cmd.extend(["-G", "wheel"]) # Add to wheel group for sudo
    useradd_cmd.append(username)
    
    success, err, _ = _run_in_chroot(target_root, useradd_cmd, f"Create User {username}", progress_callback, timeout=30)
    if not success: return False, err, None
    
    # Set password using chpasswd - only if password was provided
    if password is not None: # Check if password exists (even if empty string, let chpasswd decide)
        chpasswd_input = f"{username}:{password}"
        success, err, _ = _run_in_chroot(target_root, ["chpasswd"], f"Set Password for {username}", progress_callback, timeout=15, pipe_input=chpasswd_input)
        if not success: 
            print(f"Warning: Failed to set password for {username} after user creation: {err}")
            # Decide if this should be a fatal error for the whole installation
            # return False, err, None # Stop installation if password set fails?
            pass # Continue for now
    else:
         print(f"Warning: No password provided for user {username}. Account created without password set.")
        
    return True, "", None

# --- Package Installation ---

def setup_repositories(target_root, repositories, progress_callback=None):
    """Setup additional repositories in the target system."""
    if not repositories:
        print("No additional repositories to setup.")
        return True, ""
    
    print(f"Setting up {len(repositories)} additional repositories...")
    errors = []
    
    for repo in repositories:
        repo_id = repo.get("id", "unknown")
        repo_name = repo.get("name", repo_id)
        repo_url = repo.get("url", "")
        
        if not repo_url:
            err_msg = f"Repository {repo_id} has no URL configured"
            print(f"Warning: {err_msg}")
            errors.append(err_msg)
            continue
        
        print(f"Setting up repository: {repo_name} ({repo_id})")
        if progress_callback:
            progress_callback(f"Setting up repository: {repo_name}...", None)
        
        # Handle different repository types
        if repo_id == "flathub":
            # Flathub is handled by Flatpak setup, skip here
            continue
        elif repo_url.endswith(".repo"):
            # DNF repository file
            repo_cmd = ["dnf", "config-manager", "--add-repo", repo_url, f"--installroot={target_root}"]
        elif repo_url.endswith(".rpm"):
            # RPM package containing repository configuration
            repo_cmd = ["dnf", "install", "-y", repo_url, f"--installroot={target_root}"]
        else:
            # Generic repository URL - create repo file manually
            repo_file_path = os.path.join(target_root, f"etc/yum.repos.d/{repo_id}.repo")
            try:
                os.makedirs(os.path.dirname(repo_file_path), exist_ok=True)
                with open(repo_file_path, 'w') as f:
                    f.write(f"""[{repo_id}]
name={repo_name}
baseurl={repo_url}
enabled=1
gpgcheck=0
""")
                print(f"Created repository file: {repo_file_path}")
                continue
            except Exception as e:
                err_msg = f"Failed to create repository file for {repo_id}: {e}"
                print(f"ERROR: {err_msg}")
                errors.append(err_msg)
                continue
        
        # Execute repository setup command
        success, err, _ = _run_command(repo_cmd, f"Setup repository {repo_name}", progress_callback, timeout=120)
        if not success:
            err_msg = f"Failed to setup repository {repo_name}: {err}"
            print(f"ERROR: {err_msg}")
            errors.append(err_msg)
        else:
            print(f"Successfully setup repository: {repo_name}")
    
    final_error = "\n".join(errors) if errors else ""
    return len(errors) == 0, final_error

def install_packages_enhanced(target_root, package_config, progress_callback=None):
    """Enhanced package installation with custom repositories and package selection.
    
    package_config should contain:
    - packages: list of package names to install
    - repositories: list of additional repositories to setup
    - flatpak_enabled: whether to install and setup Flatpak
    - flatpak_packages: list of flatpak package IDs to install
    - minimal_install: whether to perform minimal installation
    """
    
    # --- Root Check --- 
    if os.geteuid() != 0:
        err = "install_packages_enhanced must be run as root."
        print(f"ERROR: {err}")
        return False, err
    
    print("Starting enhanced package installation...")
    
    # Get configuration
    packages = package_config.get("packages", [])
    repositories = package_config.get("repositories", [])
    flatpak_enabled = package_config.get("flatpak_enabled", False)
    flatpak_packages = package_config.get("flatpak_packages", [])
    minimal_install = package_config.get("minimal_install", False)
    keep_cache = package_config.get("keep_cache", True)
    
    print(f"Packages to install: {len(packages)}")
    print(f"Additional repositories: {len(repositories)}")
    print(f"Flatpak enabled: {flatpak_enabled}")
    print(f"Flatpak packages to install: {len(flatpak_packages)}")
    print(f"Minimal installation: {minimal_install}")
    
    # --- Setup Additional Repositories First ---
    if repositories:
        if progress_callback:
            progress_callback("Setting up additional repositories...", 0.1)
        success, err = setup_repositories(target_root, repositories, progress_callback)
        if not success:
            print(f"Warning: Some repositories failed to setup: {err}")
            # Continue anyway, as base installation might still work
    
    # --- Install Packages ---
    if progress_callback:
        progress_callback("Installing packages...", 0.2)
    
    if minimal_install:
        # For minimal install, use only core packages
        packages = ["@core", "kernel", "grub2-efi-x64", "grub2-pc", "NetworkManager"]
        print("Minimal installation: using core packages only")
    elif not packages:
        # Use default package list if none specified
        packages = [
            "@core", "kernel", 
            "grub2-efi-x64", "grub2-efi-x64-modules", "grub2-pc", "efibootmgr", 
            "grub2-common", "grub2-tools",
            "shim-x64", "shim",
            "linux-firmware", "NetworkManager", "systemd-resolved", 
            "bash-completion", "dnf-utils"
        ]
        print("Using default package list")
    
    success, err = _install_packages_dnf_impl(target_root, packages, progress_callback, keep_cache)
    if not success:
        return False, err
    
    # --- Setup Flatpak if enabled ---
    if flatpak_enabled:
        if progress_callback:
            progress_callback("Setting up Flatpak...", 0.85)
        
        success, err = setup_flatpak(target_root, progress_callback)
        if not success:
            print(f"Warning: Flatpak setup failed: {err}")
            # Don't fail the entire installation for Flatpak issues
        
        # --- Install Flatpak packages ---
        if flatpak_packages:
            if progress_callback:
                progress_callback("Installing Flatpak applications...", 0.9)
            
            success, err = install_flatpak_packages(target_root, flatpak_packages, progress_callback)
            if not success:
                print(f"Warning: Some Flatpak packages failed to install: {err}")
                # Don't fail the entire installation for Flatpak package issues
    
    if progress_callback:
        progress_callback("Package installation complete.", 1.0)
    
    print("Enhanced package installation completed successfully.")
    return True, ""

def _install_packages_dnf_impl(target_root, packages, progress_callback=None, keep_cache=True):
    """Implementation of DNF package installation with progress tracking."""
    
    # --- Filter out problematic packages --- 
    filtered_packages = []
    for pkg in packages:
        # Filter out almalinux-* packages that conflict with oreon-*
        if pkg.startswith("almalinux-"):
            print(f"Filtering out conflicting package: {pkg}")
            continue
        filtered_packages.append(pkg)
    
    packages = filtered_packages
    print(f"Installing {len(packages)} packages after filtering")
    
    # --- Get Release Version --- 
    os_info = get_os_release_info()
    releasever = os_info.get("VERSION_ID")
    if not releasever:
        print("Warning: Could not detect OS VERSION_ID. Falling back to default.")
        releasever = "40" # Default fallback
    print(f"Using release version: {releasever}")
    
    # Build DNF command with package exclusions
    dnf_cmd = [
        "dnf", 
        "install", 
        "-y", 
        "--nogpgcheck", 
        f"--installroot={target_root}",
        f"--releasever={releasever}",
        f"--setopt=install_weak_deps=False",
        "--exclude=firefox",
        "--exclude=redhat-flatpak-repo", 
        "--exclude=almalinux-*",
        "--exclude=steam",
        "--exclude=lutris",
        "--exclude=wine",
        "--exclude=libreoffice*",
        "--exclude=oreon-*",
        "--setopt=tsflags=noscripts",  # Skip problematic scriptlets
        "--setopt=installonly_limit=0",  # Don't limit kernel installations
        "--setopt=keepcache=1" if keep_cache else "--setopt=keepcache=0"
    ]
    
    if not keep_cache:
        dnf_cmd.append("--setopt=keepcache=0")
    
    dnf_cmd.extend(packages)

    print(f"Executing DNF installation: {' '.join(shlex.quote(c) for c in dnf_cmd[:10])}... ({len(packages)} packages)")
    if progress_callback:
        progress_callback("Starting DNF package installation...", 0.0)
        
    # --- Execute DNF and Stream Output --- 
    process = None
    stderr_output = ""
    try:
        process = subprocess.Popen(
            dnf_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        # Progress tracking patterns
        download_progress_re = re.compile(r"^Downloading Packages:.*?\[\s*(\d+)%\]")
        install_progress_re = re.compile(r"^(Installing|Updating|Upgrading|Cleanup|Verifying)\s*:.*?\s+(\d+)/(\d+)\s*$")
        total_packages_re = re.compile(r"Total download size:.*Installed size:.* Package count: (\d+)")

        total_packages = 0
        packages_processed = 0
        current_phase = "Initializing"
        last_fraction = 0.0
        
        # Read stdout line by line
        if process.stdout is not None:
            for line in iter(process.stdout.readline, ''):
                line_strip = line.strip()
                if not line_strip:
                    continue
                
                # Phase detection
                if "Downloading Packages" in line_strip:
                    current_phase = "Downloading"
                elif "Running transaction check" in line_strip:
                    current_phase = "Checking Transaction"
                elif "Running transaction test" in line_strip:
                    current_phase = "Testing Transaction"
                elif "Running transaction" in line_strip:
                    current_phase = "Running Transaction"
                elif line_strip.startswith("Installing") or line_strip.startswith("Updating"):
                    current_phase = "Installing"
                elif line_strip.startswith("Running scriptlet"):
                    current_phase = "Running Scriptlets"
                elif line_strip.startswith("Verifying"):
                    current_phase = "Verifying"
                elif line_strip.startswith("Installed:"):
                    current_phase = "Finalizing Installation"
                elif line_strip.startswith("Complete!"):
                    current_phase = "Complete"

                # Progress parsing
                fraction = last_fraction
                message = f"DNF: {current_phase}..."
                
                # Total package count
                match_total = total_packages_re.search(line_strip)
                if match_total:
                    total_packages = int(match_total.group(1))
                    print(f"Detected total package count: {total_packages}")

                # Download progress
                match_dl = download_progress_re.search(line_strip)
                if match_dl:
                    download_percent = int(match_dl.group(1))
                    fraction = 0.0 + (download_percent / 100.0) * 0.30
                    message = f"DNF: Downloading ({download_percent}%)..."
                     
                # Installation progress
                match_install = install_progress_re.search(line_strip)
                if match_install:
                    current_phase = match_install.group(1)
                    packages_processed = int(match_install.group(2))
                    total_packages_from_line = int(match_install.group(3))
                    
                    if total_packages_from_line > total_packages:
                        total_packages = total_packages_from_line
                    
                    if total_packages > 0:
                        phase_progress = packages_processed / total_packages
                        if current_phase in ["Installing", "Updating", "Upgrading"]:
                            fraction = 0.30 + phase_progress * 0.60
                        elif current_phase == "Verifying":
                            fraction = 0.90 + phase_progress * 0.05
                        elif current_phase == "Cleanup":
                            fraction = 0.95 + phase_progress * 0.05
                        message = f"DNF: {current_phase} ({packages_processed}/{total_packages})..."
                    else:
                        message = f"DNF: {current_phase} (package {packages_processed})..."
                        fraction = 0.30

                # Clamp fraction
                fraction = max(0.0, min(fraction, 0.99))
                last_fraction = fraction
                
                if progress_callback:
                    progress_callback(message, fraction)

                # Check if process exited
                if process.poll() is not None:
                    print("DNF process completed while reading output.")
                    break
        else:
            raise RuntimeError("process.stdout is None; cannot read DNF output")
                
        # Wait for process completion
        process.stdout.close()
        return_code = process.wait(timeout=60)
        
        # Read stderr
        if process.stderr:
            stderr_output = process.stderr.read()
            process.stderr.close()
        
        if return_code != 0:
            stderr_text = stderr_output.strip() if stderr_output else ""
            
            # Check for specific error types
            if "no match for group package" in stderr_text.lower():
                error_msg = f"DNF installation failed: Group package not found. This may be due to missing repositories or package groups. Error: {stderr_text}"
            elif "prein scriptlet" in stderr_text.lower():
                error_msg = f"DNF installation failed: PREIN scriptlet error. This may be due to package conflicts. Error: {stderr_text}"
            elif "conflicts" in stderr_text.lower():
                error_msg = f"DNF installation failed: Package conflicts detected. Error: {stderr_text}"
            else:
                error_msg = f"DNF installation failed (rc={return_code}). Stderr:\n{stderr_text}"
            
            print(f"ERROR: {error_msg}")
            if progress_callback:
                progress_callback(error_msg, last_fraction)
            return False, error_msg
        else:
            print("SUCCESS: DNF package installation completed.")
            if progress_callback:
                progress_callback("DNF package installation completed successfully.", 0.8)
            return True, ""
            
    except FileNotFoundError:
        err = "Command not found: dnf. Cannot install packages."
        print(f"ERROR: {err}")
        if progress_callback:
            progress_callback(err, 0.0)
        return False, err
    except subprocess.TimeoutExpired:
        err = "Timeout expired during DNF execution."
        print(f"ERROR: {err}")
        if process:
            process.kill()
        if progress_callback:
            progress_callback(err, last_fraction)
        return False, err
    except Exception as e:
        err = f"Unexpected error during DNF execution: {e}\nStderr: {stderr_output}"
        print(f"ERROR: {err}")
        if process:
            process.kill()
        if progress_callback:
            progress_callback(err, last_fraction)
        return False, err
    finally:
        if process:
            if process.stdout and not process.stdout.closed:
                process.stdout.close()
            if process.stderr and not process.stderr.closed:
                process.stderr.close()

def setup_flatpak(target_root, progress_callback=None):
    """Setup Flatpak and add Flathub repository in the target system."""
    print("Setting up Flatpak...")
    
    if progress_callback:
        progress_callback("Installing Flatpak...", 0.0)
    
    # Install Flatpak packages (should already be installed by package selection)
    flatpak_packages = ["flatpak", "xdg-desktop-portal", "xdg-desktop-portal-gtk"]
    
    # Ensure Flatpak is installed
    for package in flatpak_packages:
        check_cmd = ["rpm", "-q", package, f"--root={target_root}"]
        result = subprocess.run(check_cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"Package {package} not found, installing...")
            install_cmd = ["dnf", "install", "-y", package, f"--installroot={target_root}"]
            success, err, _ = _run_command(install_cmd, f"Install {package}", progress_callback, timeout=300)
            if not success:
                return False, f"Failed to install {package}: {err}"
    
    if progress_callback:
        progress_callback("Adding Flathub repository...", 0.5)
    
    # Add Flathub repository
    flathub_cmd = [
        "flatpak", "remote-add", "--if-not-exists", "flathub", 
        "https://dl.flathub.org/repo/flathub.flatpakrepo"
    ]
    
    success, err, _ = _run_in_chroot(target_root, flathub_cmd, "Add Flathub repository", progress_callback, timeout=60)
    if not success:
        return False, f"Failed to add Flathub repository: {err}"
    
    # Enable Flatpak user installations
    if progress_callback:
        progress_callback("Configuring Flatpak...", 0.8)
    
    # Create systemd user service directory
    systemd_user_dir = os.path.join(target_root, "etc/systemd/user/default.target.wants")
    try:
        os.makedirs(systemd_user_dir, exist_ok=True)
    except Exception as e:
        print(f"Warning: Failed to create systemd user directory: {e}")
    
    print("Flatpak setup completed successfully.")
    return True, ""

def install_flatpak_packages(target_root, flatpak_packages, progress_callback=None):
    """Install Flatpak packages in the target system."""
    if not flatpak_packages:
        print("No Flatpak packages to install.")
        return True, ""
    
    print(f"Installing {len(flatpak_packages)} Flatpak packages...")
    errors = []
    
    for i, package in enumerate(flatpak_packages):
        if progress_callback:
            progress = i / len(flatpak_packages)
            progress_callback(f"Installing Flatpak package: {package}...", progress)
        
        print(f"Installing Flatpak package: {package}")
        
        # Install flatpak package system-wide
        install_cmd = [
            "flatpak", "install", "-y", "--system", "flathub", package
        ]
        
        success, err, _ = _run_in_chroot(target_root, install_cmd, f"Install Flatpak {package}", progress_callback, timeout=300)
        if not success:
            err_msg = f"Failed to install Flatpak package {package}: {err}"
            print(f"ERROR: {err_msg}")
            errors.append(err_msg)
        else:
            print(f"Successfully installed Flatpak package: {package}")
    
    final_error = "\n".join(errors) if errors else ""
    success = len(errors) == 0
    
    if success:
        print("All Flatpak packages installed successfully.")
    else:
        print(f"Flatpak installation completed with {len(errors)} errors.")
    
    return success, final_error

# Keep the original function for backward compatibility
def install_packages_dnf(target_root, progress_callback=None):
    """Legacy function - installs base packages using DNF --installroot."""
    
    # Use default package configuration
    package_config = {
        "packages": [
            "@core", "kernel", 
            "grub2-efi-x64", "grub2-efi-x64-modules", "grub2-pc", "efibootmgr", 
            "grub2-common", "grub2-tools",
            "shim-x64", "shim",
            "linux-firmware", "NetworkManager", "systemd-resolved", 
            "bash-completion", "dnf-utils"
        ],
        "repositories": [],
        "flatpak_enabled": False,
        "minimal_install": False,
        "keep_cache": False
    }
    
    return install_packages_enhanced(target_root, package_config, progress_callback)

# --- Move NetworkManager Enable --- 
# We need a function that uses _run_in_chroot (and thus _run_command for root check)
def enable_network_manager(target_root, progress_callback=None):
    """Enables NetworkManager service in the target system via chroot."""
    if progress_callback:
        progress_callback("Enabling NetworkManager service...", 0.96) # Example fraction
    
    nm_enable_cmd = ["systemctl", "enable", "NetworkManager.service"]
    success, err, _ = _run_in_chroot(target_root, nm_enable_cmd, "Enable NetworkManager Service", progress_callback=None, timeout=30)
    if not success: 
        warning_msg = f"Warning: Failed to enable NetworkManager service: {err}"
        print(warning_msg)
        if progress_callback: progress_callback(warning_msg, 0.97) # Update UI with warning
        # Continue installation even if service enabling fails? Let's return True but log warning.
        return True, warning_msg # Indicate success overall, but pass warning
    else:
        print("Successfully enabled NetworkManager service.")
        if progress_callback: progress_callback("NetworkManager service enabled.", 0.97)
        return True, ""

# --- Bootloader Installation ---
# Installation logic is in install_logic.py

def install_bootloader_in_container(target_root, primary_disk, efi_partition_device, progress_callback=None):
    """Installs GRUB2 for UEFI (with Secure Boot) or legacy BIOS. Delegates to install_logic."""
    from install_logic import install_bootloader
    return install_bootloader(target_root, primary_disk, efi_partition_device, progress_callback)



def cleanup_efi_mount(target_root):
    """Clean up the EFI mount after bootloader installation is complete."""
    efi_mount_point = os.path.join(target_root, "boot/efi")
    
    if os.path.ismount(efi_mount_point):
        print(f"Cleaning up EFI mount: {efi_mount_point}")
        try:
            subprocess.run(["sync"], check=False, timeout=5)
            umount_cmd = ["umount", efi_mount_point]
            result = subprocess.run(umount_cmd, capture_output=True, text=True, check=True, timeout=30)
            print(f"Successfully unmounted EFI partition: {efi_mount_point}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to unmount EFI partition {efi_mount_point}: {e.stderr.strip()}")
            try:
                lazy_umount_cmd = ["umount", "-l", efi_mount_point]
                subprocess.run(lazy_umount_cmd, capture_output=True, text=True, check=True, timeout=15)
                print(f"Lazy unmount successful for EFI partition: {efi_mount_point}")
            except Exception as lazy_e:
                print(f"Warning: Lazy unmount also failed for EFI partition: {lazy_e}")
        except Exception as e:
            print(f"Warning: Error during EFI mount cleanup: {e}")
    else:
        print(f"EFI partition not mounted, no cleanup needed: {efi_mount_point}")

# --- Service Management Helpers --- 
def _manage_service(action, service_name):
    """Helper to start or stop a systemd service."""
    if action not in ["start", "stop"]:
        return False, f"Invalid service action: {action}"
    
    cmd = ["systemctl", action, service_name]
    # Use _run_command, assumes root check handled there
    success, err, _ = _run_command(cmd, f"{action.capitalize()} service {service_name}")
    if not success:
         print(f"Warning: Failed to {action} service {service_name}: {err}")
         # Don't make this fatal? Might prevent cleanup.
         # return False, err 
    return success, err

def _stop_service(service_name):
    print(f"Attempting to stop service: {service_name}...")
    return _manage_service("stop", service_name)

def _start_service(service_name):
    print(f"Attempting to start service: {service_name}...")
    return _manage_service("start", service_name)

# --- LVM Deactivation Helper --- 
def _deactivate_lvm_on_disk(disk_device, progress_callback=None):
    """Attempts to find and deactivate LVM VGs associated with a disk and its partitions."""
    print(f"Checking for and deactivating LVM on {disk_device} and its partitions...")
    if progress_callback:
        progress_callback(f"Checking LVM on {disk_device}...", None) # Text only update

    devices_to_check = set([disk_device])
    vg_names_found = set()
    all_success = True
    errors = []

    # 1. Find partitions of the main disk
    try:
        lsblk_cmd = ["lsblk", "-n", "-o", "PATH", "--raw", disk_device]
        print(f"  Running: {' '.join(shlex.quote(c) for c in lsblk_cmd)}")
        lsblk_result = subprocess.run(lsblk_cmd, capture_output=True, text=True, check=False, timeout=10)
        if lsblk_result.returncode == 0:
            found_paths = [line.strip() for line in lsblk_result.stdout.split('\n') if line.strip() and line.strip() != disk_device]
            print(f"  Found potential partition paths via lsblk: {found_paths}")
            devices_to_check.update(found_paths)
        else:
            print(f"  Warning: lsblk failed for {disk_device} (rc={lsblk_result.returncode}), checking only base device for PVs.")
    except Exception as e:
        print(f"  Warning: Error running lsblk to find partitions for {disk_device}: {e}")
        # Continue with just the base disk_device

    # 2. Find VGs associated with each device (disk + partitions)
    print(f"  Checking devices for LVM PVs: {list(devices_to_check)}")
    for device in devices_to_check:
        try:
            pvs_cmd = ["pvs", "--noheadings", "-o", "vg_name", "--select", f"pv_name={device}"]
            # Use subprocess directly here as _run_command adds too much noise for non-errors
            print(f"    Checking PV on {device}...")
            result = subprocess.run(pvs_cmd, capture_output=True, text=True, check=False, timeout=10)
            
            if result.returncode == 0:
                vgs = set(line.strip() for line in result.stdout.splitlines() if line.strip())
                if vgs:
                     print(f"      Found VGs on {device}: {vgs}")
                     vg_names_found.update(vgs)
            elif "No physical volume found" in result.stderr or "No PVs found" in result.stdout:
                # This is expected if the device isn't an LVM PV
                pass
            else:
                 # Real error running pvs
                 err_msg = f"Failed to run pvs for {device}: {result.stderr.strip()}"
                 print(f"    Warning: {err_msg}")
                 errors.append(err_msg)
                 all_success = False # Mark as potentially incomplete
                 
        except Exception as e:
             err_msg = f"Unexpected error checking PV on {device}: {e}"
             print(f"    ERROR: {err_msg}")
             errors.append(err_msg)
             all_success = False
             
    if not vg_names_found:
         print(f"  No LVM Volume Groups found associated with {disk_device} or its partitions.")
         return True, "" # Not an error if no VGs found

    # 3. Deactivate all found VGs
    print(f"  Found unique LVM VGs to deactivate: {vg_names_found}. Attempting deactivation...")
    for vg_name in vg_names_found:
         vgchange_cmd = ["vgchange", "-an", vg_name]
         # Use _run_command here as deactivation failure is important
         vg_success, vg_err, _ = _run_command(vgchange_cmd, f"Deactivate VG {vg_name}")
         if not vg_success:
             print(f"    Warning: Failed to deactivate VG {vg_name}: {vg_err}")
             errors.append(f"Failed to deactivate VG {vg_name}: {vg_err}")
             all_success = False
         else:
              print(f"    Successfully deactivated VG {vg_name}.")
              time.sleep(0.5) # Small delay after deactivation
              
    if progress_callback:
         status = "Deactivation complete." if all_success and not errors else "Deactivation attempted, some errors occurred."
         progress_callback(f"LVM Check on {disk_device}: {status}", None)
         
    final_error_str = "\n".join(errors)
    return all_success, final_error_str

# --- Device Mapper Removal Helper --- 
def _remove_dm_mappings(disk_device, progress_callback=None):
    """Attempts to find and remove device-mapper mappings for LVM LVs on a disk."""
    print(f"Checking for and removing LVM device-mapper mappings associated with {disk_device}...")
    if progress_callback:
        progress_callback(f"Removing DM mappings for {disk_device}...", None)

    devices_to_check = set([disk_device])
    vg_names_found = set()
    lvs_to_remove = set() # Store LV paths like /dev/vg/lv or /dev/mapper/vg-lv
    all_success = True
    errors = []

    # 1. Find partitions (same logic as _deactivate_lvm_on_disk)
    try:
        lsblk_cmd = ["lsblk", "-n", "-o", "PATH", "--raw", disk_device]
        lsblk_result = subprocess.run(lsblk_cmd, capture_output=True, text=True, check=False, timeout=10)
        if lsblk_result.returncode == 0:
            devices_to_check.update([p.strip() for p in lsblk_result.stdout.split('\n') if p.strip()])
    except Exception:
        pass # Ignore errors, just use base disk device

    # 2. Find VGs associated with each device
    for device in devices_to_check:
        try:
            pvs_cmd = ["pvs", "--noheadings", "-o", "vg_name", "--select", f"pv_name={device}"]
            result = subprocess.run(pvs_cmd, capture_output=True, text=True, check=False, timeout=10)
            if result.returncode == 0:
                vg_names_found.update(line.strip() for line in result.stdout.splitlines() if line.strip())
        except Exception as e:
            errors.append(f"Error finding VGs on {device}: {e}")
            all_success = False
            
    if not vg_names_found:
         print(f"  No LVM Volume Groups found for {disk_device}, skipping dmsetup removal.")
         return True, ""

    # 3. Find LVs within those VGs
    print(f"  Found VGs: {vg_names_found}. Checking for associated LVs...")
    for vg_name in vg_names_found:
        try:
             # Get LV paths, prefer /dev/mapper/ format if possible, else /dev/vg/lv
             lvs_cmd = ["lvs", "--noheadings", "-o", "lv_path", vg_name]
             result = subprocess.run(lvs_cmd, capture_output=True, text=True, check=False, timeout=10)
             if result.returncode == 0:
                 lv_paths = set(line.strip() for line in result.stdout.splitlines() if line.strip())
                 if lv_paths:
                      print(f"    Found LVs in VG {vg_name}: {lv_paths}")
                      lvs_to_remove.update(lv_paths)
             else:
                 err_msg = f"Failed to list LVs for VG {vg_name}: {result.stderr.strip()}"
                 print(f"    Warning: {err_msg}")
                 errors.append(err_msg)
                 all_success = False
        except Exception as e:
             err_msg = f"Unexpected error listing LVs for VG {vg_name}: {e}"
             print(f"    ERROR: {err_msg}")
             errors.append(err_msg)
             all_success = False
             
    if not lvs_to_remove:
        print(f"  No active LVs found in VGs {vg_names_found}.")
        return True, "\n".join(errors) # Return success even if LVs couldn't be listed, but include errors

    # 4. Remove DM mappings for found LVs
    print(f"  Attempting to remove DM mappings for LVs: {lvs_to_remove}")
    for lv_path in lvs_to_remove:
        # Need the mapper name (e.g., vg--name-lv--name) which might differ from lv_path (/dev/vg_name/lv_name)
        # We can try removing both common forms: /dev/mapper/vg-lv and the lv_path directly
        # dmsetup usually works with the name in /dev/mapper
        mapper_name = os.path.basename(lv_path)
        # Attempt removal using the basename (common case)
        dmsetup_cmd = ["dmsetup", "remove", mapper_name]
        dm_success, dm_err, _ = _run_command(dmsetup_cmd, f"Remove DM mapping {mapper_name}")
        
        if dm_success:
            print(f"    Successfully removed DM mapping {mapper_name}.")
            time.sleep(0.5) # Small delay
        else:
            # If basename fails, try the full path (less common for dmsetup remove)
            if "No such device or address" not in dm_err:
                print(f"    Attempting removal using full path {lv_path}...")
                dmsetup_cmd_fullpath = ["dmsetup", "remove", lv_path]
                dm_success_fp, dm_err_fp, _ = _run_command(dmsetup_cmd_fullpath, f"Remove DM mapping {lv_path}")
                if dm_success_fp:
                     print(f"    Successfully removed DM mapping using full path {lv_path}.")
                     time.sleep(0.5) # Small delay
                elif "No such device or address" not in dm_err_fp:
                    # Only report error if it wasn't already gone
                    err_msg = f"Failed to remove DM mapping {mapper_name} (and {lv_path}): {dm_err_fp}"
                    print(f"    Warning: {err_msg}")
                    errors.append(err_msg)
                    all_success = False # Mark as failure if any removal fails
                # else: Ignore "No such device" error on second attempt too
            # else: Ignore "No such device" error on first attempt

    if progress_callback:
        status = "DM removal complete." if all_success and not errors else "DM removal attempted, some errors occurred."
        progress_callback(f"DM Check on {disk_device}: {status}", None)

    final_error_str = "\n".join(errors)
    # Return success overall unless a removal failed with an error other than "No such device"
    return all_success, final_error_str 

# Enhanced GRUB package verification with distribution-specific handling
def verify_grub_packages(target_root):
    # Detect distribution type and set appropriate package names
    os_info = get_os_release_info(target_root=target_root)
    distro_id = os_info.get("ID", "unknown").lower()
    distro_like = os_info.get("ID_LIKE", "").lower()
    
    print(f"Detected distribution: {distro_id}, like: {distro_like}")
    
    # Set package names based on distribution
    if "fedora" in distro_id or "fedora" in distro_like:
        required_grub_packages = [
            "grub2-efi-x64",
            "grub2-efi-x64-modules", 
            "grub2-pc",
            "grub2-common",
            "grub2-tools",
            "grub2-tools-efi",
            "grub2-tools-minimal"
        ]
    elif "centos" in distro_id or "rhel" in distro_like or "rocky" in distro_like or "almalinux" in distro_like:
        required_grub_packages = [
            "grub2-efi-x64",
            "grub2-efi-x64-modules",
            "grub2-pc", 
            "grub2-common",
            "grub2-tools",
            "grub2-tools-efi",
            "grub2-tools-minimal"
        ]
    elif "ubuntu" in distro_id or "debian" in distro_like:
        required_grub_packages = [
            "grub-efi-amd64",
            "grub-efi-amd64-bin",
            "grub-common",
            "grub2-common",
            "grub-pc-bin"
        ]
    elif "arch" in distro_id or "archlinux" in distro_like:
        required_grub_packages = [
            "grub",
            "efibootmgr"
        ]
    else:
        # Generic fallback
        required_grub_packages = [
            "grub2-efi-x64",
            "grub2-tools",
            "grub2-common"
        ]
    
    print(f"Checking for GRUB packages: {required_grub_packages}")
    
    missing_packages = []
    for pkg in required_grub_packages:
        check_cmd = ["rpm", "-q", pkg, f"--root={target_root}"]
        try:
            result = subprocess.run(check_cmd, capture_output=True, text=True, check=False, timeout=10)
            if result.returncode != 0:
                # Also check with dpkg for Debian-based systems
                if "ubuntu" in distro_id or "debian" in distro_like:
                    check_cmd = ["dpkg", "-l", pkg, f"--root={target_root}"]
                    result = subprocess.run(check_cmd, capture_output=True, text=True, check=False, timeout=10)
                    if result.returncode != 0:
                        missing_packages.append(pkg)
                else:
                    missing_packages.append(pkg)
            else:
                print(f"Verified package installed: {pkg}")
        except Exception as e:
            print(f"Warning: Could not verify package {pkg}: {e}")
    
    if missing_packages:
        print(f"Missing GRUB packages: {missing_packages}")
        
        # Try to install missing packages
        try:
            print("Attempting to install missing GRUB packages...")
            if "ubuntu" in distro_id or "debian" in distro_like:
                install_cmd = ["apt-get", "install", "-y"] + missing_packages
            else:
                install_cmd = ["dnf", "install", "-y"] + missing_packages
            
            install_cmd.append(f"--installroot={target_root}")
            
            success, err, stdout = _run_in_chroot(target_root, install_cmd, "Install missing GRUB packages", timeout=300)
            if success:
                print("Successfully installed missing GRUB packages")
            else:
                return False, f"Missing required GRUB packages: {', '.join(missing_packages)}. Error: {err}", None
                
        except Exception as e:
            return False, f"Missing required GRUB packages: {', '.join(missing_packages)}. Could not install: {e}", None
    
    # If we reach here, all packages are verified or successfully installed
    print("All required GRUB packages are verified/installed")
    return True, "", None

# --- Live Environment Copy Functions ---

def copy_live_environment(target_root, progress_callback=None):
    """Copies the entire live environment to the target disk.
    
    This is much faster than installing packages and ensures the target system
    has exactly the same software as the live environment.
    """
    
    # --- Root Check --- 
    if os.geteuid() != 0:
        err = "copy_live_environment must be run as root."
        print(f"ERROR: {err}")
        return False, err
    
    print("Starting live environment copy...")
    
    if progress_callback:
        progress_callback("Preparing to copy live environment...", 0.0)

    # Validate target_root is mounted and writable before proceeding
    try:
        if not os.path.exists(target_root):
            os.makedirs(target_root, exist_ok=True)
        # Best-effort: ensure it's a mount point
        if not os.path.ismount(target_root):
            print(f"WARNING: {target_root} is not a mount point. Proceeding may copy into live FS.")
        # Write test
        test_path = os.path.join(target_root, ".copy_write_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
    except Exception as e:
        err = f"Target root not writable at {target_root}: {e}"
        print(f"ERROR: {err}")
        if progress_callback:
            progress_callback(err, 0.0)
        return False, err
    
    # Define directories to copy (exclude system-specific and volatile/mount directories)
    # Note: Excluding /mnt and /media prevents copying mounted volumes and avoids
    # recursively copying the target root (e.g., /mnt/sysimage) into itself.
    copy_directories = [
        "/bin",
        "/boot",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/opt",
        "/root",
        "/sbin",
        "/srv",
        "/usr",
        "/var",
    ]
    
    # Directories to exclude from copying (system-specific)
    exclude_directories = [
        "/dev",
        "/proc", 
        "/run",
        "/sys",
        "/tmp"
    ]
    
    # Files to exclude
    exclude_files = [
        "/etc/fstab",  # Will be regenerated
        "/etc/mtab",   # Will be regenerated
        "/etc/resolv.conf",  # Will be copied separately
        "/etc/hosts",  # Will be regenerated
        "/etc/hostname",  # Will be set by configuration
        "/etc/machine-id",  # Will be regenerated
        "/etc/adjtime",  # Will be regenerated
        "/var/lib/dbus/machine-id",  # Will be regenerated
        "/var/lib/systemd/random-seed",  # Will be regenerated
        "/var/log/*",  # Clear logs
        "/var/cache/*",  # Clear cache
        "/var/tmp/*",  # Clear temp
        "/tmp/*"  # Clear temp
    ]
    
    if progress_callback:
        progress_callback("Copying live environment to target disk...", 0.1)
    
    # Use cp with progress tracking
    total_dirs = len(copy_directories)
    completed_dirs = 0
    
    for directory in copy_directories:
        source = directory
        destination = os.path.join(target_root, directory.lstrip('/'))
        
        # If the source is a symlink (e.g., /bin -> /usr/bin), try to replicate the symlink.
        # If symlink creation is not permitted by the filesystem, fall back to copying contents.
        try:
            if os.path.islink(source):
                try:
                    link_target = os.readlink(source)
                    # Ensure parent directory exists
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    # If destination exists (as file/dir/link), remove it to replace with symlink
                    if os.path.lexists(destination):
                        try:
                            if os.path.isdir(destination) and not os.path.islink(destination):
                                shutil.rmtree(destination)
                            else:
                                os.remove(destination)
                        except Exception:
                            pass
                    os.symlink(link_target, destination)
                    completed_dirs += 1
                    progress_fraction = 0.1 + (completed_dirs / total_dirs) * 0.8
                    if progress_callback:
                        progress_callback(f"Linked {directory} -> {link_target} ({completed_dirs}/{total_dirs})", progress_fraction)
                    print(f"Created symlink {destination} -> {link_target}")
                    continue
                except OSError as e:
                    # Most commonly, the target filesystem may not permit symlinks.
                    print(f"Warning: Could not create symlink for {directory}: {e}. Falling back to copying contents.")
        except Exception as e:
            print(f"Warning: Symlink handling failed for {directory}: {e}. Falling back to copying contents.")
        
        # Create destination directory if it doesn't exist
        os.makedirs(destination, exist_ok=True)
        
        print(f"Copying {source} to {destination}...")
        
        try:
            # Use find to copy all files and directories from source to destination
            # This avoids the "copy into itself" issue
            # Use rsync when available for robust copying with symlink handling and filesystem boundary constraints
            rsync_path = shutil.which("rsync")
            if rsync_path:
                # -aHAX: archive, hardlinks, ACLs, xattrs; -S: sparse; --one-file-system: don't cross mounts
                # We copy the contents of the top-level dir, not the dir itself
                rsync_cmd = [
                    rsync_path,
                    "-aHAXS",
                    "--one-file-system",
                    f"{source}/",
                    destination,
                ]
                result = subprocess.run(rsync_cmd, capture_output=True, text=True, check=True, timeout=1800)
            else:
                # Fallback to find+cp preserving attributes and not crossing mounts
                find_cmd = [
                    "find", source,
                    "-mindepth", "1",
                    "-maxdepth", "1",
                    "-xdev",
                    "-exec", "cp", "-a", "--preserve=all", "{}", destination, ";"
                ]
                result = subprocess.run(find_cmd, capture_output=True, text=True, check=True, timeout=1800)
            
            completed_dirs += 1
            progress_fraction = 0.1 + (completed_dirs / total_dirs) * 0.8
            
            if progress_callback:
                progress_callback(f"Copied {directory} ({completed_dirs}/{total_dirs})", progress_fraction)
            
            print(f"Successfully copied {directory}")
            
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to copy {directory}: {e.stderr.strip()}"
            print(f"ERROR: {error_msg}")
            if progress_callback:
                progress_callback(error_msg, progress_fraction)
            return False, error_msg
        except subprocess.TimeoutExpired:
            error_msg = f"Timeout copying {directory} (30 minutes)"
            print(f"ERROR: {error_msg}")
            if progress_callback:
                progress_callback(error_msg, progress_fraction)
            return False, error_msg
    
    print("SUCCESS: Live environment copy completed.")
    if progress_callback:
        progress_callback("Live environment copy completed successfully.", 0.9)
    return True, ""

def setup_live_environment_post_copy(target_root, progress_callback=None):
    """Sets up the copied live environment for booting from the target disk.
    
    This function handles the post-copy setup tasks like:
    - Regenerating system-specific files
    - Setting up bootloader
    - Configuring network
    - Setting up users
    """
    
    print("Setting up live environment for target disk...")
    
    if progress_callback:
        progress_callback("Setting up target system...", 0.9)
    
    # --- Regenerate system-specific files ---
    print("Regenerating system-specific files...")
    
    # Generate new machine-id
    machine_id_path = os.path.join(target_root, "etc/machine-id")
    try:
        if os.path.exists(machine_id_path):
            os.remove(machine_id_path)
        # systemd will generate a new machine-id on first boot
        print("Removed old machine-id (will be regenerated on first boot)")
    except Exception as e:
        print(f"Warning: Could not remove machine-id: {e}")
    
    # Generate new dbus machine-id
    dbus_machine_id_path = os.path.join(target_root, "var/lib/dbus/machine-id")
    try:
        if os.path.exists(dbus_machine_id_path):
            os.remove(dbus_machine_id_path)
        # dbus will generate a new machine-id on first boot
        print("Removed old dbus machine-id (will be regenerated on first boot)")
    except Exception as e:
        print(f"Warning: Could not remove dbus machine-id: {e}")
    
    # Clear systemd random seed
    random_seed_path = os.path.join(target_root, "var/lib/systemd/random-seed")
    try:
        if os.path.exists(random_seed_path):
            os.remove(random_seed_path)
        print("Removed old random seed (will be regenerated on first boot)")
    except Exception as e:
        print(f"Warning: Could not remove random seed: {e}")
    
    # Clear logs
    log_dirs = [
        os.path.join(target_root, "var/log"),
        os.path.join(target_root, "var/cache"),
        os.path.join(target_root, "var/tmp"),
        os.path.join(target_root, "tmp")
    ]
    
    for log_dir in log_dirs:
        try:
            if os.path.exists(log_dir):
                # Remove contents but keep directory
                for item in os.listdir(log_dir):
                    item_path = os.path.join(log_dir, item)
                    try:
                        if os.path.isfile(item_path):
                            os.remove(item_path)
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                    except Exception as e:
                        print(f"Warning: Could not remove {item_path}: {e}")
                print(f"Cleared {log_dir}")
        except Exception as e:
            print(f"Warning: Could not clear {log_dir}: {e}")
    
    # --- Copy essential files from host ---
    print("Copying essential files from host...")
    
    # Copy resolv.conf
    host_resolv = "/etc/resolv.conf"
    target_resolv = os.path.join(target_root, "etc/resolv.conf")
    try:
        if os.path.exists(host_resolv):
            shutil.copy2(host_resolv, target_resolv)
            print("Copied resolv.conf from host")
    except Exception as e:
        print(f"Warning: Could not copy resolv.conf: {e}")
    
    # --- Ensure essential directories exist ---
    essential_dirs = [
        os.path.join(target_root, "proc"),
        os.path.join(target_root, "sys"),
        os.path.join(target_root, "dev"),
        os.path.join(target_root, "run"),
        os.path.join(target_root, "tmp")
    ]
    
    for dir_path in essential_dirs:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception as e:
            print(f"Warning: Could not create {dir_path}: {e}")
    
    print("Live environment setup complete.")
    if progress_callback:
        progress_callback("Live environment setup complete.", 1.0)
    
    return True, ""

def generate_fstab_for_target(target_root):
    """Generate a basic /etc/fstab inside the target based on currently mounted target filesystems.
    Uses UUIDs for reliability and includes entries for /, /boot, and /boot/efi if present.
    """
    try:
        fstab_path = os.path.join(target_root, "etc/fstab")
        print(f"Generating fstab at {fstab_path} ...")

        # Query all mounts under target_root
        findmnt_cmd = [
            "findmnt", "-rn", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", f"--target={target_root}"
        ]
        result = subprocess.run(findmnt_cmd, capture_output=True, text=True, check=False, timeout=10)
        if result.returncode != 0 or not result.stdout.strip():
            return False, f"Could not enumerate mounts for {target_root}: {result.stderr.strip()}"

        entries = []
        for line in result.stdout.splitlines():
            try:
                source, target, fstype, options = (field.strip() for field in line.split())
            except ValueError:
                # Some fields might contain spaces; fall back to a more robust split
                parts = line.split()
                if len(parts) < 3:
                    continue
                source, target, fstype = parts[:3]
                options = ",".join(parts[3:]) if len(parts) > 3 else "defaults"

            if not target.startswith(target_root.rstrip("/")):
                continue

            # Translate host mount path to target path (strip target_root prefix)
            rel_mount = target[len(target_root):] or "/"

            # Skip pseudo filesystems
            if fstype in {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "efivarfs"}:
                continue

            # Resolve UUID for block devices only
            mount_device = source
            uuid = None
            if source.startswith("/dev/"):
                try:
                    uuid_res = subprocess.run(["blkid", "-o", "value", "-s", "UUID", source],
                                              capture_output=True, text=True, check=False, timeout=5)
                    if uuid_res.returncode == 0 and uuid_res.stdout.strip():
                        uuid = uuid_res.stdout.strip()
                except Exception as e:
                    print(f"Warning: blkid failed for {source}: {e}")

            if uuid:
                device_field = f"UUID={uuid}"
            else:
                device_field = mount_device

            # Choose safe default options
            if fstype == "vfat":
                opts = "rw,relatime,fmask=0077,dmask=0077,codepage=437,iocharset=iso8859-1,shortname=mixed,errors=remount-ro"
                dump_pass = "0 2"
            else:
                opts = "rw,relatime"
                dump_pass = "0 1" if rel_mount == "/" else "0 2"

            entries.append(f"{device_field}\t{rel_mount}\t{fstype}\t{opts}\t{dump_pass}")

        if not entries:
            return False, "No eligible mounts found to generate fstab"

        # Ensure containing directory exists
        os.makedirs(os.path.dirname(fstab_path), exist_ok=True)
        with open(fstab_path, "w") as f:
            f.write("# Generated by Centrio Installer\n")
            for e in sorted(entries, key=lambda s: 0 if s.split()[1] == "/" else 1):
                f.write(e + "\n")

        print(f"fstab generated with {len(entries)} entries")
        return True, ""
    except Exception as e:
        return False, f"Failed to generate fstab: {e}"

def install_packages_on_live_copy(target_root, package_config, progress_callback=None):
    """Installs additional packages on top of the copied live environment.
    
    This function is similar to install_packages_enhanced but optimized for
    a live environment copy where the base system is already present.
    """
    
    # --- Root Check --- 
    if os.geteuid() != 0:
        err = "install_packages_on_live_copy must be run as root."
        print(f"ERROR: {err}")
        return False, err
    
    print("Installing additional packages on live environment copy...")
    
    # Get configuration
    packages = package_config.get("packages", [])
    repositories = package_config.get("repositories", [])
    flatpak_enabled = package_config.get("flatpak_enabled", False)
    flatpak_packages = package_config.get("flatpak_packages", [])
    
    print(f"Additional packages to install: {len(packages)}")
    print(f"Additional repositories: {len(repositories)}")
    print(f"Flatpak enabled: {flatpak_enabled}")
    print(f"Flatpak packages to install: {len(flatpak_packages)}")
    
    # --- Setup Additional Repositories First ---
    if repositories:
        if progress_callback:
            progress_callback("Setting up additional repositories...", 0.1)
        success, err = setup_repositories(target_root, repositories, progress_callback)
        if not success:
            print(f"Warning: Some repositories failed to setup: {err}")
            # Continue anyway, as base system is already present
    
    # --- Install Additional Packages ---
    if packages:
        if progress_callback:
            progress_callback("Installing additional packages...", 0.2)
        
        # Use DNF to install additional packages (not the full system)
        success, err = _install_packages_dnf_impl(target_root, packages, progress_callback, keep_cache=True)
        if not success:
            return False, err
    
    # --- Setup Flatpak if enabled ---
    if flatpak_enabled:
        if progress_callback:
            progress_callback("Setting up Flatpak...", 0.85)
        
        success, err = setup_flatpak(target_root, progress_callback)
        if not success:
            print(f"Warning: Flatpak setup failed: {err}")
            # Don't fail the entire installation for Flatpak issues
        
        # --- Install Flatpak packages ---
        if flatpak_packages:
            if progress_callback:
                progress_callback("Installing Flatpak applications...", 0.9)
            
            success, err = install_flatpak_packages(target_root, flatpak_packages, progress_callback)
            if not success:
                print(f"Warning: Some Flatpak packages failed to install: {err}")
                # Don't fail the entire installation for Flatpak package issues
    
    if progress_callback:
        progress_callback("Additional package installation complete.", 1.0)
    
    print("Additional package installation completed successfully.")
    return True, ""