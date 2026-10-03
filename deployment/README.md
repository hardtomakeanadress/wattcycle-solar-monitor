# Cabana Raspberry Pi installation

Verified on **3 October 2026**. One custom running application provides the solar
dashboard and Bluetooth climate collection in the same Python process. Other
application directories on the Pi contain older rollout snapshots. The
[source inventory](source-inventory.md) accounts for all 27 retrieved source and
service files, including the original reader and two historical deployment scripts.

## Verified host

| Item | Observed configuration |
| --- | --- |
| Board | Raspberry Pi 2 Model B Rev 1.1 |
| Processor | Four ARMv7 cores, armv7l; reported maximum frequency 900 MHz |
| Memory | Approximately 920 MiB usable by Linux |
| Storage | 29.1 GiB SD device, with 28.6 GiB root and 512 MiB boot partitions |
| OS | Raspbian GNU/Linux 13 (trixie), 32-bit armhf userspace |
| Kernel | 6.18.39+rpt-rpi-v7 |
| Python | 3.13.5; application uses the standard library |
| Controller | M-2430N, slave 1, 9600 baud, 8N1, CH340 USB/RS485 adapter |
| Climate sensor | Xiaomi LYWSD03MMC, using existing BlueZ gatttool |
| Application | ~/WattCycle-RS485/dashboard/server.py |
| Service | User service cabana-solar, enabled, with user lingering enabled |
| HTTP | Port 8080; trusted network or Tailscale access |
| Database | ~/WattCycle-RS485/dashboard/history.sqlite3 |
| Collection | Solar every 60 seconds; climate every 600 seconds |

The webpage measures the running host rather than copying this table. Its storage
figures describe the filesystem holding the configured database. Usable memory
and filesystem capacity differ from hardware marketing capacity.

## Services, packages and configuration

The custom unit is [cabana-solar.service](cabana-solar.service). The
[Bluetooth drop-in template](bluetooth-sensor.conf.example) replaces the installed
sensor address with a placeholder. Other relevant services are provided by the
OS: bluetooth, tailscaled, ssh, NetworkManager, wpa_supplicant, avahi-daemon and
systemd-timesyncd. The enabled mpris-proxy and user sockets are distribution
components, not separate solar projects.

- [Installed packages, versions and architectures](packages-20261003.tsv).
- [Enabled system services, sockets and timers](system-units-20261003.txt).
- [Enabled user services and sockets](user-units-20261003.txt).
- [Boot configuration](boot-config-20261003.txt).
- [Module configuration](module-options-20261003.txt).

These inventories describe the inspected Pi; they are not instructions to install
every package on a new machine. Standard services remain maintained by their
packages. Climate collection uses gatttool from bluez and stdbuf from coreutils.
Power/thermal flags use the existing vcgencmd from raspi-utils-core; if unavailable,
only those flags are unknown. No new runtime dependency was added for the Pi panel.

## Recreate the application

Set up Raspberry Pi OS for the board, a Pi user, networking and SSH with fresh
credentials. Keep the verified [controller wiring](../docs/hardware.md). These
commands describe a replacement installation; do not start a second serial reader
alongside an existing dashboard.

```sh
git clone https://github.com/hardtomakeanadress/wattcycle-solar-monitor.git ~/WattCycle-RS485
cd ~/WattCycle-RS485
python3 -m unittest discover -s dashboard -p 'test_*.py' -v
sudo usermod -aG dialout "$USER"
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp deployment/cabana-solar.service ~/.config/systemd/user/
```

Sign in again after changing groups. For the optional climate sensor, verify the
Bluetooth adapter, bluetooth service, gatttool and stdbuf are available. Copy the
drop-in and replace REPLACE_WITH_SENSOR_ADDRESS with the sensor address before
starting. Omit the drop-in when no sensor is used.

```sh
mkdir -p ~/.config/systemd/user/cabana-solar.service.d
cp deployment/bluetooth-sensor.conf.example ~/.config/systemd/user/cabana-solar.service.d/bluetooth-sensor.conf
# Edit the copied file locally; never commit the actual address.
```

Start the service after serial group membership has taken effect:

```sh
systemctl --user daemon-reload
systemctl --user enable --now cabana-solar
systemctl --user status cabana-solar --no-pager
curl -fsS http://127.0.0.1:8080/api/status
curl -fsS http://127.0.0.1:8080/api/system
```

This template preserves the installed WattCycle-RS485 directory and cabana-solar
unit name. The [generic guide](../docs/deployment.md) uses the names
wattcycle-solar-monitor and wattcycle-solar; choose one convention. Non-default
serial/database settings can be supplied with a user-service environment drop-in,
using the [documented variables](../README.md#configuration).

Reauthenticate Tailscale with the owner's account on the replacement Pi. Do not
copy authentication keys or Tailscale state into this repository. The dashboard
has no login or TLS; keep port 8080 on a trusted network/VPN without public router
port forwarding.

## Updates, data and recovery

Historical measurements are private and are not in Git. A replacement Pi starts
with empty history unless a private backup is restored. Use the
[SQLite backup procedure](../docs/deployment.md#backups), including both samples
and sensor_samples, and keep an off-device copy.

Before replacing code, back up the database and application files. Stop
cabana-solar during replacement, preserve dashboard/history.sqlite3 and the
private sensor drop-in, then restart. Check fresh solar status, sensor status,
/api/system and chart history. Roll back application files if needed; do not
replace the live database with an older copy during a code rollback.

```sh
systemctl --user status cabana-solar --no-pager
journalctl _SYSTEMD_USER_UNIT=cabana-solar.service -n 80 --no-pager
loginctl show-user "$USER" -p Linger
df -h .
vcgencmd get_throttled
```

The system journal selector above works on this Pi, where journalctl --user finds
no user journal files. Controller failures retry at the next minute. The Pi
information endpoint is independent and performs no serial/Bluetooth reads.

No additional custom project was found in the accessible user home, /opt, /srv or
/usr/local. Root's private home and crontab required a password and were not
inspected. SSH keys, Wi-Fi credentials, Tailscale state, machine-specific boot
command-line identifiers, databases and private deployment reports are excluded.
