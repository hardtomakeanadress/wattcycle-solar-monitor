# Pi source inventory — 3 October 2026

All 27 source, documentation and service files retrieved from the active application,
its historical deployment directories and its user service configuration are accounted
for below. `app/` means `~/WattCycle-RS485/`; `units/` means
`~/.config/systemd/user/`. Byte-identical older files already in Git are linked to
their commits instead of duplicating them. Unique old files are archived as text.
The runnable current application is in the repository root and `dashboard/`.

Historical rollout scripts are for reference only: they contain obsolete hashes and
paths and must not be run against the current installation. The old Pi README is
also historical; use [the current setup guide](README.md). The fixed-configuration
reader is retained here to document exactly what was running; the public root
reader adds environment configuration with identical defaults and serial behavior.

Excluded runtime artifacts: SQLite histories, bytecode, live deployment reports,
SSH keys, shell history, credentials, Wi-Fi profiles, Tailscale state and private
addresses. No additional custom project was found in the accessible Pi user home, `/opt`, `/srv`
or `/usr/local`; root-owned private files could not be inspected without a password.
Distribution package source is not vendored; package versions and service inventories
are recorded alongside this document.

| Pi source path | Public location | Treatment |
| --- | --- | --- |
| `app/bluetooth-20260928/backup/index.html` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/d8f247669f493422993684f8c94b1520ace40a35/dashboard/index.html) | Exact content match |
| `app/bluetooth-20260928/backup/server.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/642ff4f92484db10a5f942e2d91c2a4c7c71c785/dashboard/server.py) | Exact content match |
| `app/bluetooth-20260928/candidate/bluetooth-sensor.conf` | [Sensor drop-in template](bluetooth-sensor.conf.example) | Sensor address replaced with placeholder |
| `app/bluetooth-20260928/candidate/bluetooth_sensor.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/455ab9e60007d17008cd27d01689eb19227712f1/dashboard/bluetooth_sensor.py) | Exact content match |
| `app/bluetooth-20260928/candidate/index.html` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/455ab9e60007d17008cd27d01689eb19227712f1/dashboard/index.html) | Exact content match |
| `app/bluetooth-20260928/candidate/server.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/455ab9e60007d17008cd27d01689eb19227712f1/dashboard/server.py) | Exact content match |
| `app/bluetooth-20260928/deploy.py` | [Archived source](source-archive-20261003/bluetooth-20260928/deploy.py.txt) | Private account, network and sensor identifiers replaced where present; historical Python stored as text |
| `app/dashboard/README.md` | [Archived source](source-archive-20261003/dashboard/README.md) | Private account, network and sensor identifiers replaced where present; historical Python stored as text |
| `app/dashboard/bluetooth_sensor.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/d8da8e0a4a4487e8d1fcec99fccb6f61867c0bc9/dashboard/bluetooth_sensor.py) | Exact content match |
| `app/dashboard/cabana-solar.service` | [Service](cabana-solar.service) | Exact content match |
| `app/dashboard/index.html` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/d8da8e0a4a4487e8d1fcec99fccb6f61867c0bc9/dashboard/index.html) | Exact content match |
| `app/dashboard/server.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/d8da8e0a4a4487e8d1fcec99fccb6f61867c0bc9/dashboard/server.py) | Exact content match |
| `app/dashboard/test_dashboard.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/e6be7a62df5d869b3e1991892ec2bb6358ccbe90/dashboard/test_dashboard.py) | Exact content match |
| `app/dashboard/test_frontend.js` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/e6be7a62df5d869b3e1991892ec2bb6358ccbe90/dashboard/test_frontend.js) | Exact content match |
| `app/dashboard/test_reader.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/e6be7a62df5d869b3e1991892ec2bb6358ccbe90/dashboard/test_reader.py) | Exact content match |
| `app/dashboard/test_retention.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/e6be7a62df5d869b3e1991892ec2bb6358ccbe90/dashboard/test_retention.py) | Exact content match |
| `app/read_controller.py` | [Archived source](source-archive-20261003/read_controller.py.txt) | Private account, network and sensor identifiers replaced where present; historical Python stored as text |
| `app/sensor-history-20260929/backup/bluetooth_sensor.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/455ab9e60007d17008cd27d01689eb19227712f1/dashboard/bluetooth_sensor.py) | Exact content match |
| `app/sensor-history-20260929/backup/index.html` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/455ab9e60007d17008cd27d01689eb19227712f1/dashboard/index.html) | Exact content match |
| `app/sensor-history-20260929/backup/server.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/455ab9e60007d17008cd27d01689eb19227712f1/dashboard/server.py) | Exact content match |
| `app/sensor-history-20260929/candidate/bluetooth_sensor.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/d8da8e0a4a4487e8d1fcec99fccb6f61867c0bc9/dashboard/bluetooth_sensor.py) | Exact content match |
| `app/sensor-history-20260929/candidate/deploy_cabana_sensor_history.py` | [Archived source](source-archive-20261003/sensor-history-20260929/candidate/deploy_cabana_sensor_history.py.txt) | Private account, network and sensor identifiers replaced where present; historical Python stored as text |
| `app/sensor-history-20260929/candidate/index.html` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/d8da8e0a4a4487e8d1fcec99fccb6f61867c0bc9/dashboard/index.html) | Exact content match |
| `app/sensor-history-20260929/candidate/server.py` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/d8da8e0a4a4487e8d1fcec99fccb6f61867c0bc9/dashboard/server.py) | Exact content match |
| `app/time-zoom-20260926/backup/index.html` | [Existing Git version](https://github.com/hardtomakeanadress/wattcycle-solar-monitor/blob/642ff4f92484db10a5f942e2d91c2a4c7c71c785/dashboard/index.html) | Exact content match |
| `units/cabana-solar.service` | [Service](cabana-solar.service) | Exact content match |
| `units/cabana-solar.service.d/bluetooth-sensor.conf` | [Sensor drop-in template](bluetooth-sensor.conf.example) | Sensor address replaced with placeholder |
