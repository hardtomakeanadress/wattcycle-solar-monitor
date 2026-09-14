# Reference manuals and image attribution

These files document the tested hardware and communication protocol. They
retain their respective owners' rights; the project's MIT license does not
apply to the PDFs or retailer photograph. Original sources are listed below.
The wiring SVG is an original project illustration of the documented signals.

## Manuals

| Local file | Source / scope | Retrieved |
| --- | --- | --- |
| [WattCycle 30 A MPPT manual](manuals/WattCycle-30A-MPPT-User-Manual.pdf) | WattCycle official manual; M2420/M2430/M2440; RJ12 diagram in section 9, printed page 8 (PDF page 5) | 2026-09-14 |
| [Helios N-series manual](manuals/Helios-MPPT-N-Series-User-Manual.pdf) | Manufacturer CDN; covers M2430N family; RJ12 diagram in section 11, printed page 10 (PDF page 6) | 2026-09-14 |
| [Matching MPPT Modbus protocol](manuals/HQST-MPPT-MODBUS-Protocol.pdf) | Distributed for HQST controllers, mirrored by Device.report; matching telemetry map, not a verified Helios-hosted protocol | 2026-09-13 |

Original download locations:

- WattCycle: <https://cdn.shopify.com/s/files/1/0812/7420/8552/files/WattCycle_30A_MPPT_12V_24V_Solar_Charge_Controller_with_Bluetooth_User_Manual.pdf>, linked from <https://www.wattcycle.com/pages/downloads>.
- Helios: <https://yunpan.cdn.site.joinf.com/4953695959689190/2022/08/8Jm3Gb3Y6B.pdf>.
- Matching protocol: <https://device.report/m/f7478c66f5451bc15e250c04e6beab0269f54fd92659617f82bcf8d06fd19865.pdf>.

Copies are stored unchanged. The HQST document has inconsistencies; the
[protocol guide](protocol.md) distinguishes observed behavior from untested
settings. The presence of a manual does not establish compatibility with every
model it describes or prove an OEM relationship.

## Adapter photograph

- Local file: [optimus-usb-rs485.jpg](images/optimus-usb-rs485.jpg), 800 × 800 pixels.
- Source: Optimus Digital product 13279, reference `0104110000090720`.
- Product: <https://www.optimusdigital.ro/ro/interfata-convertoare-usb-la-serial/13279-convertor-usb-la-rs485.html>.
- Original image: <https://static.optimusdigital.ro/70298-thickbox_default/convertor-usb-la-rs485.jpg>.
- Retrieved: 2026-09-14. Retailer branding and the original image are preserved.

The owner identified this product as the adapter in use. The retailer photo
illustrates the product; it does not record the installed cable or terminal
polarity. See [hardware specifications](hardware.md) for measured identity and
unpublished ratings.
