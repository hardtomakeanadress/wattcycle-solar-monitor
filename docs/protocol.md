# WattCycle M2430 / M-2430N RS485 Modbus RTU protocol

This map comes from a working WattCycle 30 A MPPT controller that returns the
ASCII model `M-2430N`. Its main telemetry has been read successfully using Modbus
RTU with valid CRC checks. This is a record of observed read behavior, not a
guarantee that every controller sold as M2430 implements the same firmware.

## Wire format

- 9600 baud, 8 data bits, no parity, 1 stop bit.
- Slave address 1 on the tested controller.
- Function `0x03` reads holding registers.
- Function `0x04` returned exception `01` (unsupported function).
- Register fields are big-endian; CRC-16/Modbus is transmitted low byte first.
- Addresses below are hexadecimal PDU addresses, not 4xxxx display numbers.

## Register map

| Address | Meaning / conversion |
| --- | --- |
| `000C–0013` | Model: 16 ASCII bytes, padded with spaces |
| `0014`, `0015` | Software/hardware version; raw values / 100 in matching map |
| `0100` | Nominal system voltage |
| `0101` | Controller-estimated battery percentage |
| `0102` | Battery volts: raw × 0.1 |
| `0103` | Charging amps: raw × 0.01 |
| `0104` | Charging watts |
| `0105` | Controller temperature in high byte, battery-temperature field in low byte; sign-magnitude |
| `0106–0108` | Load volts × 0.1, amps × 0.01, watts |
| `0109` | PV volts: raw × 0.1 |
| `010A–010C` | Daily peak charging W, generated Wh, consumed Wh |
| `010D` | Load state in high byte; charge state in low byte |
| `010E` | Fault bits; displayed numerically without unverified bit labels |
| `010F` | Operating days |
| `0110–0111` | Cumulative generated Wh; reader preserves raw words |
| `0201–0203` | Address, configured system voltage, battery type; reader preserves raw values |

Charge states: 0 idle, 1 open, 2 MPPT, 3 equalizing, 4 boost, 5 float,
6 limited. Unknown state values are labeled `unknown`.

For each temperature byte, bit 7 indicates a negative value and bits 0–6 hold
the magnitude. It is not an ordinary signed 8-bit two's-complement field.

The matching document describes battery types 0 flooded, 1 sealed, 2 gel,
3 lithium and voltage setting 255 as automatic. A lithium selection does not
establish exact chemistry or validate charging setpoints.

The cumulative energy words have not been independently tested for rollover
or word order, so the application does not present a decoded lifetime total.

## Example verified exchange

Read nominal system voltage, register `0x0100`, count 1:

```text
TX 01 03 01 00 00 01 85 F6
RX 01 03 02 00 18 B8 4E
```

The response value `0x0018` is 24 V. The final two bytes are the Modbus CRC.
This packet is also used as a regression fixture.

## Reference documents and compatibility limits

- [WattCycle 30 A MPPT product page](https://www.wattcycle.com/products/wattcycle-30a-mppt-12v-24v-solar-charge-controller)
- [WattCycle downloads](https://www.wattcycle.com/pages/downloads)
- [Matching MPPT Modbus protocol distributed for HQST, mirrored by Device.report (PDF)](https://device.report/m/f7478c66f5451bc15e250c04e6beab0269f54fd92659617f82bcf8d06fd19865.pdf)

The HQST document's telemetry layout matches the observed main fields; this
does not establish the original manufacturer or compatibility with all HQST
products. The document also contains inconsistencies, including a charging
power row labeled in amperes. The reader interprets that field as watts based
on its described purpose and agreement with measured voltage × current.

Do not substitute an SRNE register map solely because some identification
addresses match: in this device, `0x0100` is nominal system voltage, while the
SRNE map considered during discovery uses that location for battery percentage.

No configuration-write behavior, charging setpoints, load switching, firmware
updates, or connector pinout has been validated here. The reader issues only
read requests and checks the model before decoding telemetry.
