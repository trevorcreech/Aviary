#!/usr/bin/env python3
"""Offline Wi-Fi provisioning for Aviary. No Internet connectivity test is used."""

import argparse
import grp
import hashlib
import html
import http.server
import ipaddress
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import socketserver
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

LOG = logging.getLogger("aviary-wifi")
NETWORK = ipaddress.ip_network("192.168.4.0/24")
GATEWAY = "192.168.4.1"
HOSTS = {GATEWAY, "aviary.setup", "captive.apple.com",
         "connectivitycheck.gstatic.com", "connectivitycheck.android.com",
         "www.msftconnecttest.com", "www.msftncsi.com", "detectportal.firefox.com"}
STATE_DIR = Path("/var/lib/aviary-wifi-recovery")
PROFILE_DIR = Path("/etc/NetworkManager/system-connections")
RUNTIME_DIR = Path("/run/aviary-wifi-recovery")


def private_write(path, text):
    """Atomic root-private writes; never put passwords in process arguments."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def key_value(value):
    # GLib KeyFile escaping. Escaping every space also preserves trailing spaces.
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t").replace(" ", "\\s"))


def validate_credentials(ssid, password, security, hidden):
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("Enter a Wi-Fi name of 1–32 bytes, without control characters.")
    if security not in {"wpa", "open"}:
        raise ValueError("Choose password protected or open Wi-Fi.")
    if security == "wpa" and not (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)):
        raise ValueError("Enter a Wi-Fi password of 8–63 printable characters.")
    return ssid, password if security == "wpa" else "", security, bool(hidden)


def client_profile(profile_uuid, ssid, password, security="wpa", hidden=False):
    ssid, password, security, hidden = validate_credentials(ssid, password, security, hidden)
    result = (f"[connection]\nid=Aviary setup {profile_uuid[:8]}\nuuid={profile_uuid}\n"
              "type=wifi\nautoconnect=false\nautoconnect-priority=50\n\n"
              # NM accepts a byte list; this also handles SSIDs like '12;34;'
              # without their being confused with the legacy byte-list syntax.
              f"[wifi]\nmode=infrastructure\nssid={''.join(str(byte) + ';' for byte in ssid.encode('utf-8'))}\n"
              f"hidden={'true' if hidden else 'false'}\n")
    if security == "wpa":
        result += f"\n[wifi-security]\nkey-mgmt=wpa-psk\npsk={key_value(password)}\n"
    return result + "\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"


def ap_profile(config):
    result = (f"[connection]\nid=Aviary recovery\nuuid={config['ap_uuid']}\n"
            f"type=wifi\ninterface-name={config['interface']}\nautoconnect=false\n\n"
            f"[wifi]\nmode=ap\nband=bg\nchannel={int(config.get('channel', 6))}\n"
            f"ssid={key_value(config['ssid'])}\npowersave=2\nap-isolation=1\n")
    if not config.get("open_setup", False):
        result += f"\n[wifi-security]\nkey-mgmt=wpa-psk\nproto=rsn;\npsk={key_value(config['password'])}\n"
    return result + (f"\n[ipv4]\nmethod=manual\naddress1={GATEWAY}/24\nnever-default=true\n\n"
                     "[ipv6]\nmethod=disabled\n")


class Firewall:
    """Own one nftables table. Never alter unrelated firewall rules."""
    def __init__(self, config):
        self.config = config
        self.mode = None

    def rules(self, mode):
        interface = self.config["interface"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", interface):
            raise ValueError("Invalid Wi-Fi interface")
        incoming, forwarding, redirect = [], [], []
        if mode == "hotspot":
            incoming = [f'iifname "{interface}" meta nfproto ipv4 udp dport 67 accept',
                        f'iifname "{interface}" ip saddr {NETWORK} ip daddr {GATEWAY} udp dport 53 accept',
                        f'iifname "{interface}" ip saddr {NETWORK} ip daddr {GATEWAY} tcp dport {{ 53, 8088 }} accept',
                        f'iifname "{interface}" counter drop']
            redirect = [f'iifname "{interface}" ip saddr {NETWORK} tcp dport 80 redirect to :8088']
        elif mode == "untrusted":
            incoming = [f'iifname "{interface}" ct direction reply accept',
                        f'iifname "{interface}" ct state related accept',
                        f'iifname "{interface}" udp sport 67 udp dport 68 accept',
                        f'iifname "{interface}" udp sport 547 udp dport 546 accept',
                        f'iifname "{interface}" icmpv6 type {{ nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert }} accept',
                        f'iifname "{interface}" counter drop']
        elif mode != "trusted":
            raise ValueError("Invalid firewall mode")
        if mode != "trusted":
            forwarding = [f'iifname "{interface}" counter drop', f'oifname "{interface}" counter drop']
        incoming.append("tcp dport 8088 counter drop")
        def chain(name, kind, hook, priority, rules):
            return (f"chain {name} {{ type {kind} hook {hook} priority {priority}; policy accept;\n"
                    + "\n".join(rule + ";" for rule in rules) + "\n}\n")
        return ("add table inet aviary_recovery\ndelete table inet aviary_recovery\n"
                "table inet aviary_recovery {\n"
                + chain("input", "filter", "input", -10, incoming)
                + chain("forward", "filter", "forward", -10, forwarding)
                + chain("portal", "nat", "prerouting", -100, redirect) + "}\n")

    def set(self, mode):
        if mode == self.mode:
            return
        result = subprocess.run(["/usr/sbin/nft", "-f", "-"], input=self.rules(mode),
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise RuntimeError("Recovery firewall could not be installed")
        self.mode = mode

    def connected(self, profile_uuid):
        self.set("untrusted" if self.config.get("open_setup", False)
                 and profile_uuid not in self.config.get("trusted_uuids", []) else "trusted")


class NetworkManager:
    def __init__(self, config, profile_dir=PROFILE_DIR, state_dir=STATE_DIR):
        self.config = config
        self.interface = config["interface"]
        self.profile_dir = Path(profile_dir)
        self.pending_path = Path(state_dir) / "pending.json"

    @staticmethod
    def command(*arguments, timeout=15):
        try:
            result = subprocess.run(["/usr/bin/nmcli", *arguments], capture_output=True,
                                    text=True, timeout=timeout, env={**os.environ, "LC_ALL": "C.UTF-8"})
        except subprocess.TimeoutExpired:
            raise RuntimeError("NetworkManager timed out") from None
        if result.returncode:
            # Do not propagate command output into logs or responses.
            raise RuntimeError("NetworkManager operation failed")
        return result.stdout.strip()

    def status(self):
        lines = self.command("-g", "GENERAL.STATE,GENERAL.CON-UUID,IP4.ADDRESS",
                             "device", "show", self.interface).splitlines()
        return {"uuid": lines[1] if len(lines) > 1 else "",
                "connected": bool(lines and lines[0].startswith("100 ") and len(lines) > 2
                                  and any(re.match(r"\d+\.\d+\.\d+\.\d+/", line) for line in lines[2:]))}

    def activate(self, profile_uuid):
        self.command("--wait", "35", "connection", "up", "uuid", profile_uuid,
                     "ifname", self.interface, timeout=42)
        state = self.status()
        if not state["connected"] or state["uuid"] != profile_uuid:
            raise RuntimeError("Wi-Fi did not receive an address")

    def deactivate_ap(self):
        if self.status()["uuid"] == self.config["ap_uuid"]:
            try:
                self.command("--wait", "10", "connection", "down", "uuid", self.config["ap_uuid"])
            except RuntimeError:
                pass

    def add_candidate(self, credentials):
        profile_uuid = str(uuid.uuid4())
        path = self.profile_dir / f"aviary-setup-{profile_uuid}.nmconnection"
        private_write(self.pending_path, json.dumps({"uuid": profile_uuid}))
        try:
            private_write(path, client_profile(profile_uuid, *credentials))
            self.command("connection", "load", str(path))
        except Exception:
            path.unlink(missing_ok=True)
            self.pending_path.unlink(missing_ok=True)
            raise
        return profile_uuid

    def commit_candidate(self, profile_uuid):
        self.command("connection", "modify", "uuid", profile_uuid, "connection.autoconnect", "yes",
                     "connection.autoconnect-priority", "50")
        self.pending_path.unlink(missing_ok=True)

    def remove_candidate(self, profile_uuid):
        # Only the UUID in our private pending file can be deleted.
        if not self.pending_path.exists() or json.loads(self.pending_path.read_text())["uuid"] != profile_uuid:
            raise RuntimeError("Candidate ownership check failed")
        try:
            self.command("connection", "delete", "uuid", profile_uuid)
        except RuntimeError:
            (self.profile_dir / f"aviary-setup-{profile_uuid}.nmconnection").unlink(missing_ok=True)
            self.command("connection", "reload")
        self.pending_path.unlink(missing_ok=True)

    def recover_pending(self):
        if self.pending_path.exists():
            profile_uuid = json.loads(self.pending_path.read_text())["uuid"]
            state = self.status()
            if state["connected"] and state["uuid"] == profile_uuid:
                self.commit_candidate(profile_uuid)
            else:
                self.remove_candidate(profile_uuid)

    def saved_profiles(self):
        profiles = []
        for line in self.command("-t", "-f", "UUID,TYPE", "connection", "show").splitlines():
            profile_uuid, kind = line.rsplit(":", 1)
            if kind != "802-11-wireless" or profile_uuid == self.config["ap_uuid"]:
                continue
            fields = self.command("-g", "connection.autoconnect,connection.autoconnect-priority,802-11-wireless.mode",
                                  "connection", "show", "uuid", profile_uuid).splitlines()
            if len(fields) >= 3 and fields[0] == "yes" and fields[2] == "infrastructure":
                profiles.append((int(fields[1]), profile_uuid))
        return [item[1] for item in sorted(profiles, reverse=True)]

    def clients(self):
        try:
            result = subprocess.run(["/usr/sbin/iw", "dev", self.interface, "station", "dump"],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode:
                return True  # Preserve the setup session when client detection is uncertain.
            return "Station " in result.stdout
        except (OSError, subprocess.TimeoutExpired):
            return True

    def scan(self):
        networks = {}
        for line in self.command("-t", "--escape", "no", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                                 "ifname", self.interface, "--rescan", "no").splitlines():
            try:
                ssid, strength, security = line.rsplit(":", 2)
                if ssid and ssid != self.config["ssid"]:
                    networks[ssid] = {"ssid": ssid, "signal": int(strength), "secure": security not in {"", "--"}}
            except ValueError:
                continue
        return sorted(networks.values(), key=lambda row: -row["signal"])


class HotspotDNS:
    def __init__(self, config, runtime_dir=RUNTIME_DIR):
        self.config = config
        self.runtime_dir = Path(runtime_dir)
        self.process = None

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        config_path = self.runtime_dir / "dnsmasq.conf"
        private_write(config_path,
                      f"interface={self.config['interface']}\nlisten-address={GATEWAY}\nbind-interfaces\n"
                      "no-resolv\nno-hosts\nlocal=/#/\naddress=/#/192.168.4.1\n"
                      "dhcp-range=192.168.4.20,192.168.4.200,255.255.255.0,1h\n"
                      "dhcp-option=3,192.168.4.1\ndhcp-option=6,192.168.4.1\n"
                      f"dhcp-leasefile={self.runtime_dir}/dnsmasq.leases\n"
                      "dhcp-authoritative\nuser=nobody\ngroup=nogroup\n")
        lease = self.runtime_dir / "dnsmasq.leases"
        lease.touch(mode=0o600, exist_ok=True)
        import pwd
        os.chown(lease, pwd.getpwnam("nobody").pw_uid, grp.getgrnam("nogroup").gr_gid)
        self.process = subprocess.Popen(["/usr/sbin/dnsmasq", "--keep-in-foreground", "--pid-file=",
                                         f"--conf-file={config_path}"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        if self.process.poll() is not None:
            raise RuntimeError("Recovery DHCP/DNS could not start")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None


class Controller:
    def __init__(self, config, nm, dns, clock=time.monotonic, sleeper=time.sleep, firewall=None):
        self.config, self.nm, self.dns = config, nm, dns
        self.clock, self.sleep = clock, sleeper
        self.firewall = firewall
        self.offline_since = None
        self.last_retry = 0
        self.state = "waiting"
        self.message = "Waiting for a saved Wi-Fi network."
        self.jobs = queue.Queue(maxsize=1)
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)

    def set_state(self, state, message):
        with self.lock:
            self.state, self.message = state, message

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "message": self.message, "ssid": self.config["ssid"]}

    def submit(self, kind, credentials=None):
        with self.lock:
            if self.state != "hotspot" or not self.jobs.empty():
                raise ValueError("A connection attempt is already in progress.")
            self.jobs.put_nowait((kind, credentials))
            self.set_state("queued", "Connecting… The setup network will disappear briefly. If the connection fails, reconnect to the same Aviary network.")

    def start_hotspot(self, message="Choose a Wi-Fi network for Tulip Aviary."):
        if self.firewall:
            self.firewall.set("hotspot")
        if self.nm.status()["uuid"] != self.config["ap_uuid"]:
            self.nm.activate(self.config["ap_uuid"])
        self.dns.start()
        self.last_retry = self.clock()
        self.set_state("hotspot", message)
        LOG.info("Recovery hotspot is available")

    def connect(self, credentials):
        candidate = None
        try:
            candidate = self.nm.add_candidate(credentials)
            if self.firewall:
                self.firewall.set("untrusted")
            self.dns.stop()
            self.nm.activate(candidate)
            self.nm.commit_candidate(candidate)
            if self.firewall:
                self.firewall.connected(candidate)
        except Exception:
            if candidate:
                self.nm.remove_candidate(candidate)
            self.start_hotspot("Could not connect. Check the Wi-Fi name and password, then try again. Saved networks have been kept.")
            LOG.info("New network failed; recovery hotspot restored")
            return
        self.offline_since = None
        self.set_state("connected", "Wi-Fi connected and saved.")
        LOG.info("New Wi-Fi network connected and saved")

    def retry_saved(self):
        profiles = self.nm.saved_profiles()
        if not profiles:
            self.last_retry = self.clock()
            return
        self.set_state("connecting", "Trying saved Wi-Fi networks…")
        self.dns.stop()
        for profile_uuid in profiles:
            try:
                if self.firewall:
                    self.firewall.set("untrusted")
                self.nm.activate(profile_uuid)
                if self.firewall:
                    self.firewall.connected(profile_uuid)
            except RuntimeError:
                continue
            self.offline_since = None
            self.set_state("connected", "Reconnected to a saved Wi-Fi network.")
            LOG.info("Saved Wi-Fi network restored")
            return
        self.start_hotspot("No saved network connected. Choose a network or enter its name below.")

    def tick(self):
        status = self.nm.status()
        now = self.clock()
        if status["connected"] and status["uuid"] != self.config["ap_uuid"]:
            if self.firewall:
                self.firewall.connected(status["uuid"])
            self.dns.stop()
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                pass
            self.offline_since = None
            self.set_state("connected", "Wi-Fi connected.")
            return
        if status["connected"] and status["uuid"] == self.config["ap_uuid"]:
            if self.state not in {"hotspot", "queued"}:
                self.start_hotspot()
            else:
                self.dns.start()  # Restart a failed DHCP process without dropping the AP.
            try:
                kind, credentials = self.jobs.get_nowait()
            except queue.Empty:
                if now - self.last_retry >= self.config["retry_seconds"] and not self.nm.clients():
                    self.retry_saved()
            else:
                self.sleep(3)  # Let the HTTP response reach the phone before switching Wi-Fi.
                if kind == "connect":
                    self.connect(credentials)
                else:
                    self.retry_saved()
                    if self.state == "queued":
                        self.set_state("hotspot", "No saved Wi-Fi networks were found.")
            return
        self.dns.stop()
        if self.firewall:
            self.firewall.set("untrusted")
        if self.offline_since is None:
            self.offline_since = now
        self.set_state("waiting", "Waiting for a saved Wi-Fi network.")
        if now - self.offline_since >= self.config["grace_seconds"]:
            self.start_hotspot()


def allowed_request(headers):
    try:
        authority = headers.get("Host", "")
        host = urlsplit("http://" + authority).hostname
        if authority not in HOSTS and authority not in {f"{name}:80" for name in HOSTS}:
            return False
        address = ipaddress.ip_address(headers.get("X-Aviary-Client-IP", ""))
        origin = headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.hostname != host or parsed.port not in {None, 80}:
                return False
        return host in HOSTS and address in NETWORK
    except ValueError:
        return False


def page(controller, error=""):
    state = controller.snapshot()
    try:
        networks = controller.nm.scan()
    except RuntimeError:
        networks = []
    choices = "".join(f'<option value="{html.escape(row["ssid"], quote=True)}"></option>' for row in networks)
    token = html.escape(controller.csrf, quote=True)
    message = html.escape(error or state["message"])
    return f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Aviary Wi-Fi setup</title>
<style>body{{font:17px system-ui,sans-serif;background:#eff3ed;color:#173127;margin:0;padding:24px}}
main{{max-width:440px;margin:20px auto;background:white;border-radius:20px;padding:28px}}
h1{{margin:0 0 8px;font-size:28px}}p{{line-height:1.5}}label{{display:block;margin-top:20px}}
input,select,button{{box-sizing:border-box;width:100%;font:inherit;padding:12px;border:1px solid #99ab9d;border-radius:8px;margin-top:6px}}
button{{background:#22583e;color:white;border:0;margin-top:24px;cursor:pointer}}input[type=checkbox]{{width:auto}}
small{{color:#496452}}.status{{background:#eff3ed;border-radius:8px;padding:12px}}</style>
<main><h1>Tulip Aviary</h1><small>Wi-Fi setup · {html.escape(state['ssid'])}</small>
<p class="status" role="status">{message}</p>
<form method="post" action="/connect"><input type="hidden" name="csrf" value="{token}">
<label for="ssid">Wi-Fi network name</label><input id="ssid" name="ssid" list="networks" required autocomplete="off" maxlength="32">
<datalist id="networks">{choices}</datalist>
<label for="security">Security</label><select id="security" name="security"><option value="wpa">Password protected (WPA/WPA2 personal)</option><option value="open">Open network</option></select>
<label for="password">Wi-Fi password</label><input id="password" type="password" name="password" autocomplete="new-password" maxlength="63">
<label><input type="checkbox" name="hidden" value="yes"> Hidden network</label>
<button>Connect and save</button></form>
<form method="post" action="/retry"><input type="hidden" name="csrf" value="{token}"><button>Try saved networks</button></form>
<p><small>If connecting fails, rejoin this same Aviary hotspot and open <b>http://192.168.4.1</b>. Your saved networks are kept.</small></p></main></html>'''


class Portal(http.server.BaseHTTPRequestHandler):
    server_version = "Aviary"

    def log_message(self, *_args):
        pass  # Never log submitted credentials, client headers, or paths.

    def respond(self, status, content, content_type="text/html; charset=utf-8"):
        content = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(content)

    def available(self):
        if not allowed_request(self.headers):
            self.respond(403, "Setup is available only on the Aviary recovery network.")
            return False
        if self.server.controller.snapshot()["state"] not in {"hotspot", "queued", "connecting"}:
            self.respond(503, "Recovery Wi-Fi is not active.")
            return False
        return True

    def do_GET(self):
        if not self.available():
            return
        if self.path == "/status":
            self.respond(200, json.dumps(self.server.controller.snapshot()), "application/json")
        else:
            self.respond(200, page(self.server.controller))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError()
        except ValueError:
            self.respond(400, "Invalid setup request.")
            return
        body = self.rfile.read(length)
        if not self.available():
            return
        controller = self.server.controller
        try:
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                raise ValueError("Invalid setup request.")
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=10)
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError("Invalid setup request.")
            fields = {key: value[0] for key, value in fields.items()}
            if not secrets.compare_digest(fields.get("csrf", ""), controller.csrf):
                self.respond(403, "Reload the setup page and try again.")
                return
            if self.path == "/connect":
                credentials = validate_credentials(fields.get("ssid", ""), fields.get("password", ""),
                                                   fields.get("security", "wpa"), fields.get("hidden") == "yes")
                controller.submit("connect", credentials)
            elif self.path == "/retry":
                controller.submit("retry")
            else:
                self.respond(404, "Not found.")
                return
        except (ValueError, UnicodeError) as exc:
            self.respond(400, page(controller, str(exc)))
            return
        self.respond(202, page(controller))


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def default_config():
    serial = ""
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("Serial"):
            serial = line.split(":", 1)[1].strip()
    suffix = serial[-4:].upper() if serial else hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()[-4:].upper()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    password = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
    return {"interface": "wlan0", "ssid": f"Aviary_{suffix}", "password": password,
            "ap_uuid": str(uuid.uuid4()), "channel": 6, "grace_seconds": 60, "retry_seconds": 300}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=STATE_DIR / "config.json")
    parser.add_argument("--guard", action="store_true", help="Block inbound Wi-Fi before NetworkManager starts")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    firewall = Firewall(config)
    if args.guard:
        firewall.set("untrusted")
        return
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    nm, dns = NetworkManager(config), HotspotDNS(config)
    nm.recover_pending()
    controller = Controller(config, nm, dns, firewall=firewall)
    socket_path = RUNTIME_DIR / "portal.sock"
    socket_path.unlink(missing_ok=True)
    server = UnixServer(str(socket_path), Portal)
    server.controller = controller
    os.chown(socket_path, 0, grp.getgrnam("caddy").gr_gid)
    os.chmod(socket_path, 0o660)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                controller.tick()
            except Exception:
                LOG.error("Recovery operation failed; will retry")
                if nm.pending_path.exists():
                    try:
                        nm.recover_pending()
                    except Exception:
                        LOG.error("Pending network recovery will be retried")
            stop.wait(5)
    finally:
        server.shutdown()
        server.server_close()
        dns.stop()
        firewall.set("untrusted")
        nm.deactivate_ap()
        socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
