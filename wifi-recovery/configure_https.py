#!/usr/bin/env python3
"""Prepare local trusted HTTPS without changing the current Wi-Fi connection."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from recovery import STATE_DIR, https_hostname, load_or_create_config, private_write, trusted_https_ready


def render_caddy(hostname, staging=False):
    hostname = https_hostname(hostname)
    directory = ("https://acme-staging-v02.api.letsencrypt.org/directory" if staging
                 else "https://acme-v02.api.letsencrypt.org/directory")
    return (Path(__file__).with_name("aviary-wifi-https.caddy").read_text()
            .replace("SETUP_HOSTNAME", hostname).replace("ACME_DIRECTORY", directory))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", default="aviary-setup.cree.ch", type=https_hostname)
    parser.add_argument("--staging", action="store_true", help="Test issuance with an untrusted staging certificate")
    parser.add_argument("--open-setup", action="store_true", help="Enable passwordless AP only after a trusted certificate is ready")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root.")
    if args.open_setup and (args.staging or not trusted_https_ready(args.hostname)):
        parser.error("A publicly trusted local certificate is required before enabling passwordless setup.")
    config = load_or_create_config()
    main_path = Path("/etc/caddy/Caddyfile")
    include = Path("/etc/caddy/aviary-wifi-https.caddy")
    original_main = main_path.read_bytes()
    original_include = include.read_bytes() if include.exists() else None
    backup = STATE_DIR / "backups" / f"https-{time.time_ns()}"
    backup.mkdir(mode=0o700, parents=True)
    private_write(backup / "Caddyfile", original_main.decode())
    if original_include is not None:
        private_write(backup / "aviary-wifi-https.caddy", original_include.decode())
    include.write_text(render_caddy(args.hostname, args.staging))
    os.chmod(include, 0o644)
    marker = b"import /etc/caddy/aviary-wifi-https.caddy"
    if marker not in original_main.splitlines():
        main_path.write_bytes(original_main.rstrip() + b"\n\n" + marker + b"\n")
    try:
        subprocess.run(["caddy", "validate", "--config", str(main_path), "--adapter", "caddyfile"],
                       check=True, capture_output=True)
        subprocess.run(["systemctl", "reload", "caddy"], check=True)
    except Exception:
        main_path.write_bytes(original_main)
        if original_include is None:
            include.unlink(missing_ok=True)
        else:
            include.write_bytes(original_include)
        raise RuntimeError("HTTPS configuration failed; the previous Caddy files were restored") from None
    config["https_hostname"] = args.hostname
    if args.open_setup:
        config["open_setup"] = True
    private_write(STATE_DIR / "config.json", json.dumps(config, indent=2) + "\n")
    print(f"HTTPS prepared for https://{args.hostname}/.")
    print(f"Tunnel hostname: {args.hostname}; service URL: http://localhost:8093; no path filter.")
    print("This tunnel listener exposes only certificate validation. It returns 404 for the setup page and Aviary data.")
    print("No Wi-Fi profile has been activated or changed.")


if __name__ == "__main__":
    main()
