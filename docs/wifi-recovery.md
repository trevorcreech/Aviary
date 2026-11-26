# Aviary recovery Wi-Fi

After 60 seconds without a working saved Wi-Fi connection, Aviary offers
**Aviary Setup**, protected by a permanent three-word passphrase. The words are
lowercase, separated by hyphens, and chosen independently with Python's
cryptographic random generator from the bundled EFF word list. Normal upgrades
preserve the passphrase. Keep it with the device as a Wi-Fi QR code or in the
family password manager; do not commit it to Git.

Join **Aviary Setup** using the passphrase or QR code. The phone can open its
captive setup window automatically. If it does not, open **http://192.168.4.1/**
or **http://aviary.setup/** while connected to that network. The setup page uses
HTTP inside the encrypted WPA2 Wi-Fi connection. It needs no Internet, public
DNS hostname, TLS certificate, certificate renewal or Cloudflare setup route.
A phone may report that the network has no Internet; stay connected for setup.

Select or type the home Wi-Fi network, enter its password and choose **Save
Credentials**. Use **Enter network name manually** for a hidden network. The
password eye button reveals or hides what you typed. Open networks need no
password. All page assets are local, so setup works offline. The hotspot
disappears while Aviary tries the connection. A successful connection is saved
for future use. If it fails, the same hotspot
returns with the same passphrase; existing profiles are retained. Personal
WPA/WPA2, hidden and open home networks are supported; enterprise authentication
and WPA3-only home networks are not supported by the form.

An Internet outage alone does not trigger recovery: a Wi-Fi association and an
IPv4 address are enough. When no phone is associated with the recovery hotspot,
Aviary tries saved networks every five minutes. An associated phone prevents
those automatic attempts from interrupting setup. **Try saved networks** starts
an immediate attempt. Returning saved networks close the hotspot once connected.

## Install or upgrade

Requires Raspberry Pi OS/Debian with NetworkManager, `dnsmasq-base`, `iw`,
`nftables`, Python 3.9+ and Caddy. From the repository:

```sh
sudo python3 wifi-recovery/install.py
```

The installer preserves a working client Wi-Fi connection. It installs an
inactive WPA2-only recovery profile, the controller and boot firewall guard,
validates/reloads Caddy and restarts recovery monitoring. Saved Wi-Fi profiles,
profile UUIDs and the trusted-network list are preserved. The recovery SSID is
migrated to **Aviary Setup**. To explicitly replace an existing setup password
with three freshly selected random words:

```sh
sudo python3 wifi-recovery/install.py --rotate-setup-password
```

The bundled EFF list has 6,216 eligible lowercase words of 3–8 letters. Three
independent choices provide approximately 37.8 bits of entropy, matching the
owner's chosen family-use policy; passphrases are 11–26 characters long.
See `wifi-recovery/WORDLIST-LICENSE.txt` for attribution and licensing.

Retrieve the setup credentials privately on the Pi:

```sh
sudo python3 -c 'import json; c=json.load(open("/var/lib/aviary-wifi-recovery/config.json")); print(c["ssid"]); print(c["password"])'
```

The configuration is mode 0600 in a root-only state directory. New home Wi-Fi
passwords are written to root-private NetworkManager keyfiles, never passed in
process arguments, returned by the portal or written to application logs.

## Data isolation

The setup passphrase grants Wi-Fi configuration access only. Recovery clients
cannot reach recordings, the normal dashboard, live microphone, SSH, forwarding
or IPv6. The WPA2 AP also enables client isolation. A dedicated nftables table
allows DHCP/DNS and the setup listener; HTTP port 80 redirects to port 8088
regardless of Host header. Other applications' firewall tables are preserved.

Caddy only proxies recovery-subnet clients to the controller's private Unix
socket and replaces the trusted client-address header. The controller checks
source address, Host, same-origin form submissions and a session CSRF token.
Connectivity probes redirect to the canonical local HTTP origin. The form is
unavailable until the password-protected hotspot is active. Credentials and
submitted passwords are never reflected into responses.

Networks added through setup keep inbound Wi-Fi access restricted, even after
reboot. Use the owner's Cloudflare sign-in for data access there. Only existing
`trusted_uuids` regain ordinary LAN access. This boundary is independent of the
setup password. The boot guard installs restrictions before NetworkManager can
connect; the controller then classifies the active profile. Cloudflare uses
Pi-initiated connections, which remain allowed.

A failed candidate can only be deleted if it belongs to the private pending
record. After a crash, a connected pending candidate is committed; a failed
candidate is removed without deleting an existing home profile.

## Removal of the previous HTTPS setup

Upgrading removes the legacy `open_setup` and `https_hostname` configuration
keys, the Caddy import/listeners for local TLS and certificate-only port 8093,
the retired HTTPS helper, and the cached certificate/key for that setup hostname.
The runtime has no TLS check, open-hotspot mode, HSTS or DHCP option 114 captive
API. Recovery always uses WPA2 and the same local HTTP addresses.

Delete the former setup-only published application route and its DNS record
from Cloudflare (for Tulip Aviary, `aviary-setup.cree.ch`). The existing website
and SSH tunnel routes continue serving their separate purposes.

## Files and checks

- Runtime: `/usr/local/lib/aviary-wifi-recovery/`
- Private configuration: `/var/lib/aviary-wifi-recovery/config.json`
- Private AP profile: `/etc/NetworkManager/system-connections/aviary-recovery.nmconnection`
- Setup proxy: `/etc/caddy/aviary-wifi-recovery.caddy`
- Controller and guard: `aviary-wifi-recovery.service`, `aviary-wifi-guard.service`
- Private backend socket: `/run/aviary-wifi-recovery/portal.sock`

```sh
python3 -m unittest discover -s tests -p test_wifi_recovery.py -v
sudo systemctl status aviary-wifi-recovery aviary-wifi-guard
sudo journalctl -u aviary-wifi-recovery
```

Tests cover recovery timing, saved-network retention/retries, failed candidate
rollback, crash recovery, passphrase bounds, WPA2-only profiles, inbound data
isolation, Host/source/Origin/CSRF boundaries and native captive redirects.
The controller scans before switching the single radio into AP mode and retains
the results for network selection and validation if NetworkManager clears its
scan cache. The form uses `Referrer-Policy: same-origin`; `no-referrer` would make
normal browser form submissions send `Origin: null` and fail the origin check.
Validate actual Caddy, nftables and inactive NetworkManager profiles on the Pi
before deployment. Coordinate a physical radio test with the owner and arm an
independent restore timer before intentionally disconnecting remote Wi-Fi.
Stopping recovery deactivates its own AP and retains inbound restrictions;
NetworkManager still manages existing saved client connections.
