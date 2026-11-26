#!/usr/bin/env python3
"""Install password-protected recovery without changing working client Wi-Fi."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from recovery import (STATE_DIR, NetworkManager, ap_profile, generate_setup_password,
                      load_or_create_config, private_write)


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def migrate_config(config, rotate_password=False):
    config = {key: value for key, value in config.items() if key not in {"https_hostname", "open_setup"}}
    config["ssid"] = "Aviary Setup"
    if rotate_password:
        config["password"] = generate_setup_password()
    ap_profile(config)
    return config


def remove_certificate(hostname):
    if not hostname:
        return
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname) or ".." in hostname:
        raise ValueError("Invalid former setup hostname")
    data = Path("/var/lib/caddy/.local/share/caddy")
    for directory in data.glob(f"certificates/*/{hostname}"):
        shutil.rmtree(directory)
    for path in (data / "ocsp").glob(f"{hostname}-*"):
        path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rotate-setup-password", action="store_true",
                        help="Replace the setup password with three newly selected random words")
    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run this installer as root.")
    for binary in ("/usr/bin/nmcli", "/usr/sbin/dnsmasq", "/usr/sbin/iw", "/usr/sbin/nft", "/usr/bin/caddy"):
        if not Path(binary).exists():
            sys.exit(f"Install the missing dependency first: {binary}")
    source = Path(__file__).resolve().parent
    destination = Path("/usr/local/lib/aviary-wifi-recovery")
    original_config = load_or_create_config()
    config = migrate_config(original_config, args.rotate_setup_password)
    config_path = STATE_DIR / "config.json"
    main_path = Path("/etc/caddy/Caddyfile")
    retired_include = Path("/etc/caddy/aviary-wifi-https.caddy")
    main = b"\n".join(line for line in main_path.read_bytes().splitlines()
                       if line.strip() != b"import /etc/caddy/aviary-wifi-https.caddy")
    marker = b"import /etc/caddy/aviary-wifi-recovery.caddy"
    if marker not in [line.strip() for line in main.splitlines()]:
        main = main.rstrip() + b"\n\n" + marker

    previous = {}
    def write(path, data, mode=0o644):
        path = Path(path)
        if path not in previous:
            previous[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        private_write(path, data.decode("utf-8"))
        os.chmod(path, mode)

    was_active = subprocess.run(["systemctl", "is-active", "--quiet", "aviary-wifi-recovery"],
                                capture_output=True).returncode == 0
    try:
        write(main_path, main.rstrip() + b"\n")
        write(Path("/etc/caddy/aviary-wifi-recovery.caddy"), (source / "aviary-wifi-recovery.caddy").read_bytes())
        run("caddy", "validate", "--config", str(main_path), "--adapter", "caddyfile")
        # Stop before updating the AP profile, including a legacy open AP.
        # A working client Wi-Fi connection is retained.
        if was_active:
            run("systemctl", "stop", "aviary-wifi-recovery")
        write(config_path, (json.dumps(config, indent=2) + "\n").encode(), 0o600)
        ap_path = Path("/etc/NetworkManager/system-connections/aviary-recovery.nmconnection")
        write(ap_path, ap_profile(config).encode(), 0o600)
        NetworkManager.command("connection", "load", str(ap_path))
        for name in ("recovery.py", "install.py", "eff-wordlist.txt", "WORDLIST-LICENSE.txt"):
            write(destination / name, (source / name).read_bytes())
        for name in ("aviary-wifi-recovery.service", "aviary-wifi-guard.service"):
            write(Path("/etc/systemd/system") / name, (source / name).read_bytes())
        write(Path("/etc/systemd/system/NetworkManager.service.d/aviary-wifi-guard.conf"),
              b"[Unit]\nRequires=aviary-wifi-guard.service\nAfter=aviary-wifi-guard.service\n")
        run("systemctl", "reload", "caddy")
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "aviary-wifi-guard.service")
        run("systemctl", "enable", "aviary-wifi-recovery.service")
        run("systemctl", "restart", "aviary-wifi-recovery.service")
    except Exception:
        for path, original in reversed(list(previous.items())):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                private_write(path, original[0].decode("utf-8"))
                os.chmod(path, original[1])
        for command in (["nmcli", "connection", "reload"], ["systemctl", "reload", "caddy"],
                        ["systemctl", "daemon-reload"]):
            subprocess.run(command, capture_output=True)
        if was_active:
            subprocess.run(["systemctl", "restart", "aviary-wifi-recovery"], capture_output=True)
        raise RuntimeError("Recovery installation failed; previous files were restored") from None

    retired_include.unlink(missing_ok=True)
    (destination / "configure_https.py").unlink(missing_ok=True)
    remove_certificate(original_config.get("https_hostname"))
    print(f"Recovery installed. SSID: {config['ssid']}. Credentials: {config_path} (root only).")
    print("Setup uses local HTTP over WPA2. The former setup-only Cloudflare route can be deleted.")


if __name__ == "__main__":
    main()
