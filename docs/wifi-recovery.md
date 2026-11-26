# Aviary recovery Wi-Fi

If `wlan0` has no working saved Wi-Fi connection for 60 seconds, Aviary starts
its own password protected `Aviary_XXXX` network. The suffix is the last four
hexadecimal digits of the Pi serial number (a machine-ID hash is the fallback).
The generated setup password remains the same across restarts and upgrades.

Join that network and open **http://192.168.4.1** (or **http://aviary.setup**).
The phone may also open its captive portal automatically. Choose or type a
Wi-Fi name, enter its password, and select **Connect and save**. Open networks
and hidden networks are supported. WPA enterprise and WPA3-only networks are
not supported by this setup form. If the phone says the hotspot has no Internet,
stay connected while entering the settings.

Known phone/computer connectivity checks receive an HTTP redirect to the local
setup page, supporting the native captive portal window. This behavior works
independently of whether the hotspot requires a Wi-Fi password. The popup still
depends on the phone's captive-network detection and saved network preferences;
the manual address remains available if it does not open.

With trusted HTTPS enabled, DHCP option 114 also advertises the standard
HTTPS captive portal API (`/captive-portal/api`, RFC 8908/8910). It reports the
secure setup URL directly to compatible phones. Legacy HTTP probes redirect
to that same HTTPS URL. The credentials form is never served or accepted over
plain HTTP while the hotspot is open.

The hotspot disappears while Aviary tries the new network. A successful
connection is saved with autoconnect enabled and priority 50. On failure, only
the new candidate profile is removed and the same setup hotspot returns.
Existing profiles are never overwritten or deleted. Rejoin the hotspot to try
again. On pre-existing trusted Wi-Fi profiles, Aviary is available at
**http://aviary.local**. Networks added through passwordless setup retain the
inbound restrictions described below; use the owner's Cloudflare sign-in there.

While the recovery hotspot has no associated devices, Aviary tries saved
autoconnect networks every five minutes, in NetworkManager priority order.
A connected phone prevents those automatic attempts from interrupting setup.
**Try saved networks** requests an immediate attempt. A returning saved network
turns off the hotspot once connected. An Internet outage alone does not start
the hotspot: association and an IPv4 address are enough.

## Install

Requires Raspberry Pi OS/Debian with NetworkManager, `dnsmasq-base`, `iw`, `nftables`, Python
3.9+, and an existing Caddy installation. From the repository:

```sh
sudo python3 wifi-recovery/install.py
```

Installation creates an inactive NetworkManager AP profile, installs a systemd
service, validates and reloads Caddy, and starts monitoring. It does not switch
away from a working Wi-Fi connection. Re-running the installer preserves the
hotspot name, password, and current connection.

Retrieve the setup credentials locally as root:

```sh
sudo python3 -c 'import json; c=json.load(open("/var/lib/aviary-wifi-recovery/config.json")); print(c["ssid"]); print(c["password"])'
```

Do not publish that file or copy credentials into Git. It is mode 0600 inside
a root-only directory. New Wi-Fi passwords are written to root-private
NetworkManager keyfiles and never passed in process arguments or logged.

## Components and boundaries

- `/usr/local/lib/aviary-wifi-recovery/recovery.py`: standard-library controller
  and HTTP setup server, supervised by `aviary-wifi-recovery.service`.
- `/var/lib/aviary-wifi-recovery/config.json`: stable AP credentials and timing.
- `/etc/NetworkManager/system-connections/aviary-recovery.nmconnection`: explicit
  activation only, 2.4 GHz, `192.168.4.1/24`, no default route. Protected by
  WPA2 by default, with an optional HTTPS-only open mode.
- A private dnsmasq instance supplies DHCP and wildcard DNS only while the AP
  is active. It does not share an upstream Internet connection.
- An nftables table restricts the recovery interface to DHCP, DNS and the setup
  listener, blocks forwarding and IPv6 access, and redirects HTTP to port 8088.
  The normal dashboard, recordings, microphone and SSH are inaccessible from
  recovery, including requests with a forged Host header.
- `/etc/caddy/aviary-wifi-recovery.caddy`: a dedicated setup listener proxies to
  a root/caddy-only Unix socket. Both Caddy and the app restrict requests to
  the recovery subnet. Setup submissions require a CSRF token and matching
  origin, and are accepted only during recovery.
- `aviary-wifi-guard.service`: installs an inbound Wi-Fi restriction before
  NetworkManager starts. The controller permits normal LAN access after it
  classifies the connected profile. Loopback Cloudflare origins are unaffected.

### Open setup is not enabled by default

The HTTPS configuration command supports enabling passwordless setup after a
publicly trusted local certificate is available. In this mode,
newly provisioned profiles keep inbound Wi-Fi access blocked, including after
reboot; only the pre-existing `trusted_uuids` regain normal LAN access. Cloudflare
continues through Pi-initiated connections. This prevents a setup user from
moving the Pi onto their own router and directly reading its LAN dashboard.

The controller opens the hotspot only when Caddy has a locally cached,
publicly trusted certificate valid for at least ten more minutes. All setup
requests redirect to the configured HTTPS hostname; plain HTTP submissions
are rejected. The listener overwrites the backend's trusted transport header.
No password or saved credentials are returned by the setup endpoints.

If a certificate is missing, untrusted, or nearing expiry after a long offline
period, recovery switches to the protected WPA2 hotspot and uses the manual
HTTP address over that encrypted Wi-Fi. The stable fallback password must be
kept with the device. Certificate renewal uses the Internet while normal Wi-Fi
works; serving a valid cached certificate does not require Internet access.
Use the known HTTPS hostname when entering credentials: an open SSID alone
does not authenticate a device, and an impostor hotspot could present a
different website.

The service restores a pending candidate after a crash: it commits it if it
is connected, otherwise removes only that candidate. It resumes an already
active AP and restarts DHCP/DNS if needed.

## Trusted setup HTTPS

Use a hostname in a domain you control. This is the local hotspot setup page;
it is not a new publicly accessible dashboard.

```sh
sudo python3 wifi-recovery/configure_https.py --hostname setup.example.net
```

This prepares a local HTTPS Caddy listener and a certificate-only loopback
origin on port 8093, saves a private configuration backup, and reloads Caddy.
It does not install or switch on the Wi-Fi recovery service. In your existing
Cloudflare tunnel, add a published application route with:

- Hostname: `setup.example.net`
- Service URL: `http://localhost:8093`
- Path: blank
- Certificate validation must be reachable without a Cloudflare Access login.

The port 8093 listener only proxies GET/HEAD requests for strict ACME challenge
paths to Caddy's HTTP challenge handler. Every other route returns 404, as do
unknown challenge tokens. It never proxies the setup page, normal dashboard,
recordings or administration endpoints. No Cloudflare DNS API key is required
on the Pi. Caddy obtains and renews the certificate through this route.

After the route resolves and issuance succeeds, this command verifies the
cached certificate against the system trust store before enabling open mode:

```sh
sudo python3 wifi-recovery/configure_https.py --hostname setup.example.net --open-setup
sudo python3 wifi-recovery/install.py
```

On the hotspot, wildcard DNS resolves the HTTPS hostname directly to the Pi at
192.168.4.1, so TLS terminates locally even without upstream connectivity. The
same hostname's public Cloudflare route only exposes certificate validation.
If needed after creating the route, force a Caddy reload to retry issuance:

```sh
sudo caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force
```

The HTTPS configuration script also supports `--staging` for issuance tests.
A staging certificate is deliberately untrusted and cannot enable open setup.

## Check and disable

```sh
python3 -m unittest discover -s tests -p test_wifi_recovery.py -v
sudo systemctl status aviary-wifi-recovery
sudo journalctl -u aviary-wifi-recovery
sudo systemctl stop aviary-wifi-recovery
```

Stopping the service deactivates its own AP if active; normal NetworkManager
autoconnection resumes. It leaves all saved Wi-Fi profiles intact and keeps
inbound Wi-Fi restricted. To completely remove this protection deliberately:

```sh
sudo systemctl disable --now aviary-wifi-recovery aviary-wifi-guard
sudo rm /etc/systemd/system/NetworkManager.service.d/aviary-wifi-guard.conf
sudo systemctl daemon-reload
sudo nft delete table inet aviary_recovery
```

Before intentionally dropping remote Wi-Fi for a hardware test, arrange an
independent timed rollback that restores the known working profile. Test DHCP,
DNS and the HTTP page with a separate Wi-Fi client, as unit tests cannot verify
radio or client captive-portal behavior.

The initial Pi verification passed the 24 unit/HTTP tests, loaded both AP
profile variants without activation, and round-tripped a Unicode/semicolon
SSID. An isolated network namespace verified DHCP offers, wildcard DNS, the
setup page, forged-host rejection, inaccessible data/SSH/stream ports, IPv6
blocking, and new-network inbound isolation. These checks did not change
`wlan0` or install the service.

The HTTPS extension passed 32 unit/HTTP tests on both Mac and Pi. A virtual
DHCP client received option 114 with the correct HTTPS API URL; protected
fallback removed that advertisement. An isolated TLS client verified the HTTPS
form with a private test certificate, rejection by the normal public trust
store, HTTP-to-HTTPS probe redirects, and restriction of the certificate-only
origin. Test keys, interfaces, firewall tables and servers were removed. The
production deployment then obtained a publicly trusted Let's Encrypt certificate
and verified it directly against the system trust store. A timed physical radio
test disconnected the working Wi-Fi, observed automatic recovery AP mode and
a healthy HTTPS setup backend, then restored the original profile. No phone
associated during that window, so the actual phone popup remains unverified.
The original website, tunnel and recording services were healthy afterward;
both boot units passed `systemd-analyze verify`.
