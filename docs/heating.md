# Battery enclosure heating

The dashboard has **Arm** and **Turn off** controls in the **Battery heating** panel.
The Pi runs the thermostat locally after arming; a browser does not need to stay
open. The current installation is **locked and disabled**, as requested by the
owner on 16 October 2026: the bulbs and enclosure sensor installation are not ready.

## Installation recorded from the owner

Six WattCycle 12 V, 100 Ah LiFePO4 batteries currently installed (owner correction,
25 October 2026), without BMS communications;
two planned 24 V, 75 W truck bulbs connected in parallel to LOAD; the existing
Xiaomi Bluetooth sensor is intended to go inside the enclosure, away from direct
bulb heat. Exact battery variant and bank wiring remain unconfirmed. Do not infer
the bank arrangement from the controller's 24 V nominal reading.

## WattCycle guidance, checked 16 October 2026

- WattCycle's [12 V 100 Ah FAQ](https://www.wattcycle.com/pages/faqs_12v-1ooah)
  lists charging at **0–50°C** and cold-charge protection below **0°C**.
- The [Group 24 specifications](https://www.wattcycle.com/products/wattcycle-12v-100ah-group-24-lifepo4-battery-1)
  give a **5°C** recovery temperature for cold-charge protection.
- Its [winter-storage advice](https://www.wattcycle.com/pages/faqs) recommends a
  dry environment at **0–25°C**, and approximately **50–70% charge for storage**.
  Storage advice is not a charging permission or a thermostat setting.

These are manufacturer statements for the cited products/guidance, not proof of
the installed batteries' BMS behavior. Confirm the exact model/manual before
commissioning. No manufacturer prescription for this external bulb heater was
found. Enclosure air can warm much faster than six batteries; air temperature
does not establish that every battery's cells are safe to charge. The software
does not interrupt solar charging and does not report charging as safe.

The owner requested five-minute heating measurements to conserve the Bluetooth
sensor battery. Do not shorten this interval without agreement.
Keep the panel compact: show mode, LOAD state, thresholds and alarms; keep the
control-key field under **Unlock controls** and detailed guidance behind **Guide**.

## Initial software behavior

These are commissioning defaults, **not WattCycle heater specifications**:

| Condition | Behavior |
| --- | --- |
| Arm requested | Verify LOAD OFF; wait for a newly received Bluetooth reading |
| Enclosure ≤5°C | Request LOAD ON and verify controller readback |
| Enclosure ≥10°C | Request LOAD OFF and verify readback |
| Between 5°C and 10°C | Retain previous heating state to avoid rapid switching |
| While armed | Bluetooth every 300 s; each new sample also verifies controller state |
| Temperature missing, invalid, sensor error, or >390 s old | Request OFF; latch alarm and disarm |
| Controller/command or history-storage error | Request OFF; latch alarm and disarm |
| Enclosure ≥25°C, below −20°C, or above sensor control range | Request OFF; latch alarm and disarm |
| Continuous heating reaches 30 minutes | Request OFF; inspection and manual re-arming required |
| Disable requested | Disarm and verify OFF; may explicitly retry a previously failed OFF |
| Service stops/restarts | Request OFF if heating owned LOAD; never resume heating automatically |

The 30-minute limit is deliberately an initial commissioning limit, not a claim
that 150 W can warm this bank within that time. Validate temperature distribution,
heater mounting, current and warm-up behavior before considering longer operation.
Missing data and alarms never cause an automatic ON retry. A failed OFF is not
silently retried: the page says **LOAD OFF unconfirmed** and requires intervention.
The 390-second age limit allows the five-minute interval plus 90 seconds of grace.
Arming still requires a new reading within 90 seconds. A 1-second supervisor loop
checks cached age and faults without additional Bluetooth measurements; actual OFF
delivery can also wait for the current bounded serial transaction.

The page uses an amber state while heating and a red alarm with explanatory text
for faults. A browser that loses contact shows output as unconfirmed; it cannot
prove the Pi stopped. Alarm recovery does not automatically re-arm. A manual SSH
OFF request disarms heating. Manual SSH ON is rejected while heating owns LOAD.

## Access and commissioning lock

`HEATING_INSTALLATION_READY` defaults to unset/false. The server rejects arming
until it is explicitly set to `1` in the service environment. Do not lift this
lock before the owner confirms installation and the operational limits are reviewed.
The UI displays **Installation pending** while locked. Deploying software alone
does not arm heating or switch the output.

The authenticated `POST /api/heating` endpoint accepts exactly `{"armed":true}`
or `{"armed":false}`. A 202 response means the request was accepted; actual mode
and output confirmation arrive through `/api/status`. It requires a bearer key,
JSON content type, bounded request body, and same-origin browser requests. No
CORS write access is enabled. All other telemetry endpoints remain read-only.

A random key is generated once at `heating-control.key` beside the SQLite file,
with permissions `0600`. On Cabana, the administrator retrieves it over SSH:

```sh
cat ~/WattCycle-RS485/dashboard/heating-control.key
```

Open **Unlock controls** and enter it in the **Control key** field. The page does not persist the key in
browser storage, return it in status, or put it in a URL. Use the control page over
Tailscale or trusted HTTPS; plain HTTP on an untrusted network exposes credentials.
No new runtime dependencies or network services are introduced.

Heating ownership/alarm metadata is atomically saved in `heating-state.json`
beside the database before any ON command. Startup uses this to request OFF after
an interrupted heating session. The armed state is never replayed. Normal climate
polling returns to 600 s when disarmed; normal solar polling remains 300 s.
Extra heating samples are retained in history. Pending buffers hold 10,080 readings
per source (seven days at one sample per minute), with existing drop/error reporting.
This buffer capacity does not change the five-minute heating measurement interval.

## Physical limits

The Pi cannot guarantee OFF if it loses power, crashes, or loses RS485 communication.
The controller may retain LOAD ON during such a failure. A plausible but misplaced
or stuck sensor can also defeat temperature supervision. Software alarms are not
independent thermal protection. No new hardware or rewiring is included in this
change. Hardware heater behavior remains untested until the installation exists.

Verification uses simulated heating/cooling, stale/missing data, control failures,
restart/shutdown recovery, authentication checks, and browser button/alarm tests.
Browser heating requests are intercepted and cannot reach physical hardware.
