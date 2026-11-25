#!/usr/bin/env python3
"""Install without interrupting the current Wi-Fi connection. Run as root."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from recovery import STATE_DIR, PROFILE_DIR, ap_profile, default_config, private_write


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
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    config_path = STATE_DIR / "config.json"
    if not config_path.exists():
        config = default_config()
        profiles = subprocess.check_output(["/usr/bin/nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"], text=True)
        config["trusted_uuids"] = [line.split(":")[0] for line in profiles.splitlines() if line.endswith(":802-11-wireless")]
        private_write(config_path, json.dumps(config, indent=2) + "\n")
    config = json.loads(config_path.read_text())
    profile_path = PROFILE_DIR / "aviary-recovery.nmconnection"
    private_write(profile_path, ap_profile(config))
    run("/usr/bin/nmcli", "connection", "load", str(profile_path))
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
