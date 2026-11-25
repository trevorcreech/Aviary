"""Recovery behavior and HTTP boundary tests; no Wi-Fi hardware is required."""

import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

SPEC = importlib.util.spec_from_file_location("recovery", Path(__file__).resolve().parents[1] / "wifi-recovery/recovery.py")
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

CONFIG = {"ap_uuid": "11111111-1111-4111-8111-111111111111", "interface": "wlan0",
          "ssid": "Aviary_ABCD", "password": "test-password", "grace_seconds": 60, "retry_seconds": 300}


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

    def status(self):
        return {"uuid": self.current or "", "connected": self.current is not None}

    def activate(self, profile_uuid):
        self.activations.append(profile_uuid)
        self.current = None
        if profile_uuid in self.fail:
            raise RuntimeError("failed")
        self.current = profile_uuid

    def prepare_ap(self, open_setup):
        self.prepared_modes.append(open_setup)
        changed = self.ap_open != open_setup
        self.ap_open = open_setup
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

    def scan(self):
        return [{"ssid": '<script>"&', "signal": 80, "secure": True}]


class FakeDNS:
    def __init__(self):
        self.running = False
        self.https = False

    def set_https(self, active):
        self.https = active

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

    def test_explicit_retry_with_no_saved_profiles_remains_available(self):
        self.hotspot()
        self.nm.profiles = []
        self.controller.submit("retry")
        self.controller.tick()
        self.assertEqual(self.controller.state, "hotspot")

    def test_passwordless_ap_requires_valid_trusted_certificate(self):
        config = {**CONFIG, "https_hostname": "setup.example.net", "open_setup": True}
        for valid in (True, False):
            with self.subTest(valid=valid):
                controller = recovery.Controller(config, self.nm, self.dns, tls_check=lambda _: valid)
                controller.start_hotspot()
                self.assertEqual(self.nm.ap_open, valid)
                self.assertEqual(controller.protected_ap, not valid)
                self.assertEqual(controller.https_active, valid)
                self.assertEqual(controller.state, "hotspot")

    def test_expiring_certificate_switches_open_ap_to_protected_recovery(self):
        config = {**CONFIG, "https_hostname": "setup.example.net", "open_setup": True}
        valid = True
        controller = recovery.Controller(config, self.nm, self.dns, clock=lambda: self.now,
                                         tls_check=lambda _: valid)
        controller.start_hotspot()
        self.assertTrue(self.nm.ap_open)
        valid = False
        self.now = 300
        controller.tick()
        self.assertFalse(self.nm.ap_open)
        self.assertTrue(controller.protected_ap)
        self.assertFalse(controller.https_active)
        self.assertFalse(self.dns.https)
        self.assertEqual(controller.snapshot()["setup_url"], "http://192.168.4.1/")


class CredentialTests(unittest.TestCase):
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


class FirewallTests(unittest.TestCase):
    def test_hotspot_redirects_web_blocks_other_services_and_forwarding(self):
        rules = recovery.Firewall(CONFIG).rules("hotspot")
        self.assertIn("tcp dport 80 redirect to :8088", rules)
        self.assertIn('iifname "wlan0" counter drop', rules)
        self.assertIn('oifname "wlan0" counter drop', rules)
        self.assertIn("meta nfproto ipv4 udp dport 67 accept", rules)
        self.assertNotIn("dport 22 accept", rules)
        self.assertNotIn("flush ruleset", rules)

    def test_open_setup_new_network_remains_isolated(self):
        config = {**CONFIG, "open_setup": True, "trusted_uuids": ["parents"]}
        firewall = recovery.Firewall(config)
        modes = []
        firewall.set = modes.append
        firewall.connected("new-network")
        firewall.connected("parents")
        self.assertEqual(modes, ["untrusted", "trusted"])
        self.assertNotIn("[wifi-security]", recovery.ap_profile(config))

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
                self.assertIn("Connect and save", body)

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

    def enable_https(self):
        self.controller.config = {**CONFIG, "https_hostname": "setup.example.net", "open_setup": True}
        self.controller.https_active = True
        self.controller.protected_ap = False

    def test_https_setup_never_serves_the_form_over_http(self):
        self.enable_https()
        for host in ("192.168.4.1", "captive.apple.com", "setup.example.net"):
            status, body, headers = self.request(headers={"Host": host})
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], "https://setup.example.net/")
            self.assertNotIn("name=\"password\"", body)
        fields = {"csrf": self.controller.csrf, "ssid": "New", "password": "a-password"}
        self.assertEqual(self.request("POST", "/connect", fields)[0], 403)
        self.assertTrue(self.controller.jobs.empty())

    def test_valid_https_form_and_submission(self):
        self.enable_https()
        headers = {"Host": "setup.example.net", "X-Aviary-Setup-Scheme": "https", "Origin": "https://setup.example.net"}
        status, body, response_headers = self.request(headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("Connect and save", body)
        self.assertIn("Strict-Transport-Security", response_headers)
        fields = {"csrf": self.controller.csrf, "ssid": "New", "password": "a-password"}
        self.assertEqual(self.request("POST", "/connect", fields, headers)[0], 202)
        self.assertEqual(self.controller.jobs.qsize(), 1)

    def test_https_origin_cannot_be_downgraded(self):
        self.enable_https()
        headers = {"Host": "setup.example.net", "X-Aviary-Setup-Scheme": "https", "Origin": "http://setup.example.net"}
        self.assertEqual(self.request(headers=headers)[0], 403)

    def test_standard_captive_portal_api_requires_https(self):
        self.enable_https()
        headers = {"Host": "setup.example.net", "X-Aviary-Setup-Scheme": "https"}
        status, body, response_headers = self.request(path="/captive-portal/api", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["Content-Type"], "application/captive+json")
        self.assertEqual(json.loads(body), {"captive": True, "user-portal-url": "https://setup.example.net/"})
        self.assertEqual(self.request(path="/captive-portal/api")[0], 302)

    def test_no_plaintext_form_on_an_unprotected_ap_without_https(self):
        self.controller.protected_ap = False
        self.assertEqual(self.request()[0], 503)


if __name__ == "__main__":
    unittest.main()
