#!/usr/bin/env python3
"""Install without interrupting the current Wi-Fi connection. Run as root."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from recovery import STATE_DIR, NetworkManager, load_or_create_config, trusted_https_ready


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def main():
    if os.geteuid() != 0:
        sys.exit("Run this installer as root.")
    for binary in ("/usr/bin/nmcli", "/usr/sbin/dnsmasq", "/usr/sbin/iw", "/usr/sbin/nft", "/usr/bin/caddy"):
        if not Path(binary).exists():
            sys.exit(f"Install the missing dependency first: {binary}")
    source = Path(__file__).resolve().parent
    destination = Path("/usr/local/lib/aviary-wifi-recovery")
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    config_path = STATE_DIR / "config.json"
    config = load_or_create_config()
    open_setup = bool(config.get("open_setup") and config.get("https_hostname")
                      and trusted_https_ready(config["https_hostname"]))
    NetworkManager(config).prepare_ap(open_setup)
    for name in ("recovery.py", "install.py"):
        shutil.copyfile(source / name, destination / name)
        os.chmod(destination / name, 0o644)
    unit = Path("/etc/systemd/system/aviary-wifi-recovery.service")
    shutil.copyfile(source / unit.name, unit)
    os.chmod(unit, 0o644)
    guard = Path("/etc/systemd/system/aviary-wifi-guard.service")
    shutil.copyfile(source / guard.name, guard)
    os.chmod(guard, 0o644)
    dropin = Path("/etc/systemd/system/NetworkManager.service.d/aviary-wifi-guard.conf")
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text("[Unit]\nRequires=aviary-wifi-guard.service\nAfter=aviary-wifi-guard.service\n")
    os.chmod(dropin, 0o644)
    caddy_include = Path("/etc/caddy/aviary-wifi-recovery.caddy")
    caddy_main = Path("/etc/caddy/Caddyfile")
    old_main = caddy_main.read_bytes()
    old_include = caddy_include.read_bytes() if caddy_include.exists() else None
    shutil.copyfile(source / caddy_include.name, caddy_include)
    os.chmod(caddy_include, 0o644)
    marker = b"import /etc/caddy/aviary-wifi-recovery.caddy"
    if marker not in old_main.splitlines():
        caddy_main.write_bytes(old_main.rstrip() + b"\n\n" + marker + b"\n")
    try:
        run("/usr/bin/caddy", "validate", "--config", str(caddy_main), "--adapter", "caddyfile")
        run("/usr/bin/systemctl", "reload", "caddy")
    except Exception:
        caddy_main.write_bytes(old_main)
        if old_include is None:
            caddy_include.unlink(missing_ok=True)
        else:
            caddy_include.write_bytes(old_include)
        raise
    run("/usr/bin/systemctl", "daemon-reload")
    run("/usr/bin/systemctl", "enable", "--now", "aviary-wifi-guard.service")
    run("/usr/bin/systemctl", "enable", "aviary-wifi-recovery.service")
    run("/usr/bin/systemctl", "restart", "aviary-wifi-recovery.service")
    print(f"Recovery installed. SSID: {config['ssid']}. Credentials: {config_path} (root only).")


if __name__ == "__main__":
    main()
