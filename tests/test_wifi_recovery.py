"""Recovery behavior and HTTP boundary tests; no Wi-Fi hardware is required."""

import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

SPEC = importlib.util.spec_from_file_location("recovery", Path(__file__).resolve().parents[1] / "wifi-recovery/recovery.py")
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
INSTALL_SPEC = importlib.util.spec_from_file_location("wifi_install", Path(__file__).resolve().parents[1] / "wifi-recovery/install.py")
installer = importlib.util.module_from_spec(INSTALL_SPEC)
with patch.dict(sys.modules, {"recovery": recovery}):
    INSTALL_SPEC.loader.exec_module(installer)

CONFIG = {"ap_uuid": "11111111-1111-4111-8111-111111111111", "interface": "wlan0",
          "ssid": "Aviary Setup", "password": "test-password", "grace_seconds": 60, "retry_seconds": 300}


class FakeNM:
    def __init__(self):
        self.current = "parents"
        self.profiles = ["parents", "old-home"]
        self.fail = set()
        self.activations = []
        self.removed = []
        self.committed = []
        self.has_clients = False
        self.ap_open = False
        self.prepared_modes = []
        self.scans = []

    def status(self):
        return {"uuid": self.current or "", "connected": self.current is not None}

    def activate(self, profile_uuid):
        self.activations.append(profile_uuid)
        self.current = None
        if profile_uuid in self.fail:
            raise RuntimeError("failed")
        self.current = profile_uuid

    def prepare_ap(self):
        self.prepared_modes.append(False)
        changed = self.ap_open
        self.ap_open = False
        return changed

    def deactivate_ap(self):
        if self.current == CONFIG["ap_uuid"]:
            self.current = None

    def add_candidate(self, credentials):
        self.credentials = credentials
        return "candidate"

    def remove_candidate(self, profile_uuid):
        self.removed.append(profile_uuid)

    def commit_candidate(self, profile_uuid):
        self.committed.append(profile_uuid)

    def saved_profiles(self):
        return list(self.profiles)

    def clients(self):
        return self.has_clients

    def scan(self, refresh=False):
        self.scans.append(refresh)
        return [{"ssid": '<script>"&', "signal": 80, "secure": True}]


class FakeDNS:
    def __init__(self):
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.nm, self.dns = FakeNM(), FakeDNS()
        self.controller = recovery.Controller(CONFIG, self.nm, self.dns, clock=lambda: self.now, sleeper=lambda _: None)

    def hotspot(self):
        self.nm.current = None
        self.controller.tick()
        self.now = 60
        self.controller.tick()
        self.assertEqual(self.nm.current, CONFIG["ap_uuid"])

    def test_healthy_wifi_does_not_need_internet_or_start_hotspot(self):
        self.controller.tick()
        self.now = 3600
        self.controller.tick()
        self.assertEqual(self.controller.state, "connected")
        self.assertEqual(self.nm.activations, [])
        self.assertFalse(self.dns.running)

    def test_disconnect_grace_and_recovery(self):
        self.nm.current = None
        self.controller.tick()
        self.now = 59
        self.controller.tick()
        self.assertEqual(self.nm.activations, [])
        self.now = 60
        self.controller.tick()
        self.assertEqual(self.controller.state, "hotspot")
        self.assertTrue(self.dns.running)

    def test_wifi_returning_during_grace_cancels_hotspot(self):
        self.nm.current = None
        self.controller.tick()
        self.now = 59
        self.nm.current = "old-home"
        self.controller.tick()
        self.now = 120
        self.controller.tick()
        self.assertEqual(self.nm.activations, [])

    def test_failed_password_restores_same_ap_and_keeps_profiles(self):
        self.hotspot()
        original = self.nm.profiles[:]
        self.nm.fail.add("candidate")
        self.controller.submit("connect", ("New network", "wrong-secret", "wpa", False))
        self.assertEqual(self.nm.current, CONFIG["ap_uuid"])
        self.controller.tick()
        self.assertEqual(self.nm.current, CONFIG["ap_uuid"])
        self.assertEqual(self.controller.state, "hotspot")
        self.assertEqual(self.nm.removed, ["candidate"])
        self.assertEqual(self.nm.profiles, original)
        self.assertTrue(self.dns.running)

    def test_successful_new_network_is_saved_and_ap_stops(self):
        self.hotspot()
        self.controller.submit("connect", ("New network", "correct-secret", "wpa", False))
        self.controller.tick()
        self.assertEqual(self.nm.committed, ["candidate"])
        self.assertEqual(self.controller.state, "connected")
        self.assertFalse(self.dns.running)

    def test_idle_hotspot_rejoins_old_network(self):
        self.hotspot()
        self.now = 359
        self.controller.tick()
        self.assertEqual(len(self.nm.activations), 1)
        self.now = 360
        self.controller.tick()
        self.assertEqual(self.nm.current, "parents")
        self.assertFalse(self.dns.running)

    def test_connected_setup_client_prevents_automatic_retry(self):
        self.hotspot()
        self.nm.has_clients = True
        self.now = 3600
        self.controller.tick()
        self.assertEqual(self.nm.activations, [CONFIG["ap_uuid"]])
        self.nm.has_clients = False
        self.controller.tick()
        self.assertEqual(self.nm.current, "parents")

    def test_all_saved_networks_fail_reopens_hotspot(self):
        self.hotspot()
        self.nm.fail.update(self.nm.profiles)
        self.now = 360
        self.controller.tick()
        self.assertEqual(self.nm.current, CONFIG["ap_uuid"])
        self.assertEqual(self.controller.state, "hotspot")

    def test_restart_with_active_hotspot_starts_dns(self):
        self.nm.current = CONFIG["ap_uuid"]
        self.controller.tick()
        self.assertTrue(self.dns.running)
        self.assertEqual(self.nm.activations, [])
        self.assertEqual(self.nm.scans, [])

    def test_hotspot_scans_before_switching_the_radio(self):
        events = []
        scan, activate = self.nm.scan, self.nm.activate
        self.nm.scan = lambda refresh=False: (events.append(("scan", refresh)), scan(refresh))[1]
        self.nm.activate = lambda profile_uuid: (events.append(("activate", profile_uuid)), activate(profile_uuid))[1]
        self.hotspot()
        self.assertEqual(events, [("scan", True), ("activate", CONFIG["ap_uuid"])])

    def test_scan_failure_still_allows_hotspot_and_manual_entry(self):
        self.nm.scan = lambda **_: (_ for _ in ()).throw(RuntimeError())
        self.hotspot()
        self.assertTrue(self.controller.protected_ap)
        self.assertEqual(self.controller.state, "hotspot")

    def test_explicit_retry_with_no_saved_profiles_remains_available(self):
        self.hotspot()
        self.nm.profiles = []
        self.controller.submit("retry")
        self.controller.tick()
        self.assertEqual(self.controller.state, "hotspot")

    def test_upgrade_reconnects_a_legacy_open_ap_with_password_protection(self):
        config = {**CONFIG, "https_hostname": "setup.example.net", "open_setup": True}
        self.nm.current = CONFIG["ap_uuid"]
        self.nm.ap_open = True
        controller = recovery.Controller(config, self.nm, self.dns)
        controller.start_hotspot()
        self.assertFalse(self.nm.ap_open)
        self.assertTrue(controller.protected_ap)
        self.assertEqual(self.nm.activations, [CONFIG["ap_uuid"]])
        self.assertEqual(controller.snapshot()["setup_url"], "http://192.168.4.1/")


class ScanTests(unittest.TestCase):
    def test_dropdown_survives_networkmanager_clearing_results_in_ap_mode(self):
        nm = recovery.NetworkManager(CONFIG)
        results = iter(["Home:40:WPA2\nHome:75:WPA2\nGuest:60:--\nAviary Setup:100:WPA2", "", ""])
        nm.command = lambda *_, **__: next(results)
        expected = [{"ssid": "Home", "signal": 75, "secure": True},
                    {"ssid": "Guest", "signal": 60, "secure": False}]
        self.assertEqual(nm.scan(refresh=True), expected)
        controller = recovery.Controller(CONFIG, nm, FakeDNS())
        controller.set_state("hotspot", "Choose Wi-Fi")
        body = recovery.page(controller)
        self.assertIn('<select id="ssid"', body)
        self.assertIn('value="Home"', body)
        self.assertNotIn('name="mode" value="manual"', body)
        self.assertEqual(nm.scan(), expected)

    def test_cached_scan_handles_driver_scan_errors(self):
        nm = recovery.NetworkManager(CONFIG)
        nm.command = lambda *_, **__: "Home:75:WPA2"
        expected = nm.scan(refresh=True)
        nm.command = lambda *_, **__: (_ for _ in ()).throw(RuntimeError())
        self.assertEqual(nm.scan(), expected)


class CredentialTests(unittest.TestCase):
    def test_setup_passphrase_is_three_words_and_fits_wpa(self):
        password = recovery.generate_setup_password()
        words = password.split("-")
        self.assertEqual(len(words), 3)
        self.assertTrue(all(3 <= len(word) <= 8 and word.isalpha() and word.islower() for word in words))
        recovery.validate_credentials("Aviary Setup", password, "wpa", False)
        self.assertEqual(recovery.default_config()["ssid"], "Aviary Setup")

    def test_missing_or_truncated_wordlist_fails_instead_of_weak_password(self):
        with patch.object(Path, "read_text", return_value="11111 acorn\n11112 maple\n"):
            with self.assertRaises(RuntimeError):
                recovery.generate_setup_password()

    def test_hotspot_cannot_be_opened_with_a_legacy_flag_or_empty_password(self):
        self.assertIn("key-mgmt=wpa-psk", recovery.ap_profile({**CONFIG, "open_setup": True}))
        with self.assertRaises(ValueError):
            recovery.ap_profile({**CONFIG, "password": ""})

    def test_profile_injection_and_special_ssids(self):
        value = recovery.client_profile("test-uuid", "12;34; café", "pass\\word ;=", "wpa", True)
        encoded = "".join(f"{byte};" for byte in "12;34; café".encode())
        self.assertIn(f"ssid={encoded}\n", value)
        self.assertIn("psk=pass\\\\word\\s;=\n", value)
        self.assertIn("hidden=true", value)
        self.assertIn("autoconnect=false", value)
        with self.assertRaises(ValueError):
            recovery.client_profile("test", "name\n[connection]", "a-password")
        with self.assertRaises(ValueError):
            recovery.client_profile("test", "name", "password\nnew-key=x")

    def test_byte_length_and_open_network_validation(self):
        with self.assertRaises(ValueError):
            recovery.validate_credentials("é" * 17, "password", "wpa", False)
        self.assertEqual(recovery.validate_credentials("café", "ignored", "open", False)[1], "")

    def test_password_is_in_private_file_never_process_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            nm = recovery.NetworkManager(CONFIG, directory, directory)
            calls = []
            nm.command = lambda *args, **kwargs: calls.append(args) or ""
            candidate = nm.add_candidate(("New Wi-Fi", "private-password", "wpa", False))
            profile = Path(directory) / f"aviary-setup-{candidate}.nmconnection"
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
            self.assertIn("psk=private-password", profile.read_text())
            self.assertNotIn("private-password", repr(calls))
            nm.commit_candidate(candidate)
            self.assertFalse(nm.pending_path.exists())

    def test_crash_recovery_commits_connected_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            nm = recovery.NetworkManager(CONFIG, directory, directory)
            recovery.private_write(nm.pending_path, json.dumps({"uuid": "candidate"}))
            nm.status = lambda: {"connected": True, "uuid": "candidate"}
            calls = []
            nm.command = lambda *args, **kwargs: calls.append(args) or ""
            nm.recover_pending()
            self.assertEqual(calls[0][:4], ("connection", "modify", "uuid", "candidate"))
            self.assertFalse(nm.pending_path.exists())

    def test_deletion_requires_pending_uuid_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            nm = recovery.NetworkManager(CONFIG, directory, directory)
            recovery.private_write(nm.pending_path, json.dumps({"uuid": "candidate"}))
            with self.assertRaises(RuntimeError):
                nm.remove_candidate("existing-home-network")


class InstallerTests(unittest.TestCase):
    def test_migration_preserves_password_profiles_and_trust(self):
        old = {**CONFIG, "ssid": "Aviary_ABCD", "open_setup": True,
               "https_hostname": "setup.example.net", "trusted_uuids": ["parents"]}
        new = installer.migrate_config(old)
        self.assertEqual(new["ssid"], "Aviary Setup")
        self.assertEqual(new["password"], old["password"])
        self.assertEqual(new["ap_uuid"], old["ap_uuid"])
        self.assertEqual(new["trusted_uuids"], ["parents"])
        self.assertNotIn("open_setup", new)
        self.assertNotIn("https_hostname", new)
        self.assertTrue(old["open_setup"])

    def test_explicit_rotation_replaces_only_setup_secret(self):
        with patch.object(installer, "generate_setup_password", return_value="acorn-maple-velvet"):
            new = installer.migrate_config(CONFIG, rotate_password=True)
        self.assertEqual(new["password"], "acorn-maple-velvet")
        self.assertEqual(new["ap_uuid"], CONFIG["ap_uuid"])

    def test_certificate_cleanup_is_limited_to_retired_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retired = root / "certificates/issuer/setup.example.net"
            other = root / "certificates/issuer/other.example.net"
            for path in (retired, other):
                path.mkdir(parents=True)
                (path / "test.key").write_text("test fixture")
            with patch.object(installer, "Path", return_value=root):
                installer.remove_certificate("setup.example.net")
            self.assertFalse(retired.exists())
            self.assertTrue((other / "test.key").exists())
        with self.assertRaises(ValueError):
            installer.remove_certificate("../other")


class FirewallTests(unittest.TestCase):
    def test_hotspot_redirects_web_blocks_other_services_and_forwarding(self):
        rules = recovery.Firewall(CONFIG).rules("hotspot")
        self.assertIn("tcp dport 80 redirect to :8088", rules)
        self.assertIn('iifname "wlan0" counter drop', rules)
        self.assertIn('oifname "wlan0" counter drop', rules)
        self.assertIn("meta nfproto ipv4 udp dport 67 accept", rules)
        self.assertNotIn("dport 22 accept", rules)
        self.assertNotIn("flush ruleset", rules)

    def test_password_protected_setup_new_network_remains_isolated(self):
        config = {**CONFIG, "trusted_uuids": ["parents"]}
        firewall = recovery.Firewall(config)
        modes = []
        firewall.set = modes.append
        firewall.connected("new-network")
        firewall.connected("parents")
        self.assertEqual(modes, ["untrusted", "trusted"])
        self.assertIn("[wifi-security]", recovery.ap_profile(config))
        self.assertNotIn("443", firewall.rules("hotspot"))

    def test_missing_trust_list_fails_closed(self):
        firewall = recovery.Firewall(CONFIG)
        modes = []
        firewall.set = modes.append
        firewall.connected("unknown-network")
        self.assertEqual(modes, ["untrusted"])

    def test_bad_interface_cannot_inject_firewall_rules(self):
        with self.assertRaises(ValueError):
            recovery.Firewall({**CONFIG, "interface": 'wlan0"; flush ruleset'}).rules("hotspot")

    def test_inbound_filter_is_installed_before_joining_new_network(self):
        events = []
        class RecordingFirewall:
            def set(self, mode):
                events.append(mode)
            def connected(self, profile_uuid):
                events.append("classified")
        nm = FakeNM()
        nm.activate = lambda profile_uuid: events.append("join")
        controller = recovery.Controller(CONFIG, nm, FakeDNS(), firewall=RecordingFirewall())
        controller.connect(("New", "password", "wpa", False))
        self.assertEqual(events, ["untrusted", "join", "classified"])


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("192.168.4.1", timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.directory.name) / "portal.sock")
        self.controller = recovery.Controller(CONFIG, FakeNM(), FakeDNS())
        self.controller.protected_ap = True
        self.controller.set_state("hotspot", "Choose Wi-Fi")
        self.server = recovery.UnixServer(self.socket_path, recovery.Portal)
        self.server.controller = self.controller
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method="GET", path="/", fields=None, headers=None):
        connection = UnixHTTP(self.socket_path)
        actual = {"Host": "192.168.4.1", "X-Aviary-Client-IP": "192.168.4.20"}
        if headers:
            actual.update(headers)
        body = urlencode(fields) if fields is not None else None
        if body is not None:
            actual["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body, actual)
        response = connection.getresponse()
        result = response.status, response.read().decode(), dict(response.getheaders())
        connection.close()
        return result

    def test_page_escapes_ssids_and_has_no_cache(self):
        status, body, headers = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn('<script>"&', body)
        self.assertIn("&lt;script&gt;&quot;&amp;", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "same-origin")

    def test_phone_connectivity_checks_redirect_to_setup(self):
        for host, path in [("captive.apple.com", "/hotspot-detect.html"),
                           ("connectivitycheck.gstatic.com", "/generate_204"),
                           ("www.msftconnecttest.com", "/connecttest.txt")]:
            with self.subTest(host=host):
                status, _, headers = self.request(path=path, headers={"Host": host})
                self.assertEqual(status, 302)
                self.assertEqual(headers["Location"], "http://192.168.4.1/")
                status, body, _ = self.request()
                self.assertEqual(status, 200)
                self.assertIn("Save Credentials", body)

    def test_lan_public_and_unexpected_host_cannot_access_setup(self):
        for headers in ({"X-Aviary-Client-IP": "192.168.1.83"}, {"X-Aviary-Client-IP": "8.8.8.8"},
                        {"X-Aviary-Client-IP": ""}, {"Host": "aviary.cree.ch"}, {"Host": "192.168.4.1/path"}):
            self.assertEqual(self.request(headers=headers)[0], 403)

    def test_forged_origin_or_csrf_cannot_change_network(self):
        fields = {"csrf": self.controller.csrf, "ssid": "New", "password": "a-password"}
        self.assertEqual(self.request("POST", "/connect", fields, {"Origin": "https://attacker.example"})[0], 403)
        fields["csrf"] = "wrong"
        self.assertEqual(self.request("POST", "/connect", fields)[0], 403)
        self.assertTrue(self.controller.jobs.empty())

    def test_submission_is_queued_and_password_is_not_reflected(self):
        fields = {"csrf": self.controller.csrf, "ssid": "New", "password": "secret-password"}
        status, body, _ = self.request("POST", "/connect", fields, {"Origin": "http://192.168.4.1"})
        self.assertEqual(status, 202)
        self.assertNotIn("secret-password", body)
        self.assertEqual(self.controller.nm.activations, [])
        self.assertEqual(self.controller.jobs.qsize(), 1)
        self.assertEqual(self.request("POST", "/connect", fields)[0], 400)

    def test_inactive_recovery_is_not_available(self):
        self.controller.set_state("connected", "Connected")
        self.assertEqual(self.request()[0], 503)

    def test_no_form_before_password_protected_ap_is_ready(self):
        self.controller.protected_ap = False
        self.assertEqual(self.request()[0], 503)

    def test_connectivity_probe_origins_cannot_submit_credentials(self):
        fields = {"csrf": self.controller.csrf, "ssid": "New", "password": "a-password"}
        headers = {"Host": "captive.apple.com", "Origin": "http://captive.apple.com"}
        self.assertEqual(self.request("POST", "/connect", fields, headers)[0], 403)
        self.assertTrue(self.controller.jobs.empty())

    def test_https_origin_cannot_submit_to_http_setup(self):
        fields = {"csrf": self.controller.csrf, "ssid": "New", "password": "a-password"}
        self.assertEqual(self.request("POST", "/connect", fields,
                                      {"Origin": "https://192.168.4.1"})[0], 403)

    def test_opaque_origin_cannot_bypass_the_same_origin_check(self):
        fields = {"csrf": self.controller.csrf, "ssid": "New", "password": "a-password"}
        self.assertEqual(self.request("POST", "/connect", fields, {"Origin": "null"})[0], 403)
        self.assertTrue(self.controller.jobs.empty())

    def test_retired_public_hostname_cannot_access_setup(self):
        self.assertEqual(self.request(headers={"Host": "aviary-setup.cree.ch"})[0], 403)

    def test_status_has_local_url_but_no_setup_password(self):
        status, body, headers = self.request(path="/status")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["setup_url"], "http://192.168.4.1/")
        self.assertTrue(state["password_protected"])
        self.assertNotIn(CONFIG["password"], body)
        self.assertNotIn("Strict-Transport-Security", headers)

    def test_selected_network_uses_scanned_security(self):
        self.controller.nm.scan = lambda: [{"ssid": "Home", "secure": True}]
        fields = {"csrf": self.controller.csrf, "ssid": "Home", "password": "", "mode": "scan"}
        self.assertEqual(self.request("POST", "/connect", fields)[0], 400)
        self.assertTrue(self.controller.jobs.empty())
        self.controller.nm.scan = lambda: [{"ssid": "Home", "secure": False}]
        self.assertEqual(self.request("POST", "/connect", fields)[0], 202)
        self.assertEqual(self.controller.jobs.get_nowait(), ("connect", ("Home", "", "open", False)))

    def test_manual_entry_supports_hidden_networks_without_extra_controls(self):
        _, body, _ = self.request(path="/?manual=1")
        self.assertIn('name="mode" value="manual"', body)
        self.assertNotIn('<select id="ssid"', body)
        fields = {"csrf": self.controller.csrf, "ssid": "Hidden", "password": "passphrase", "mode": "manual"}
        self.assertEqual(self.request("POST", "/connect", fields)[0], 202)
        self.assertEqual(self.controller.jobs.get_nowait(), ("connect", ("Hidden", "passphrase", "wpa", True)))

    def test_scan_error_does_not_queue_a_connection(self):
        self.controller.nm.scan = lambda: (_ for _ in ()).throw(RuntimeError())
        fields = {"csrf": self.controller.csrf, "ssid": "Home", "password": "passphrase", "mode": "scan"}
        status, body, _ = self.request("POST", "/connect", fields)
        self.assertEqual(status, 400)
        self.assertIn("Enter the network name manually", body)
        self.assertTrue(self.controller.jobs.empty())

    def test_local_script_has_the_same_access_boundary(self):
        status, body, headers = self.request(path="/setup.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn('password.type', body)
        self.assertEqual(self.request(path="/setup.js", headers={"X-Aviary-Client-IP": "192.168.1.83"})[0], 403)


if __name__ == "__main__":
    unittest.main()
