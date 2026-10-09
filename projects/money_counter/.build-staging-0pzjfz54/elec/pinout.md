# Pinout — money_counter

U1: Raspberry Pi Zero 2 W — 21 of 40 header pins wired.

| Phys | Pin | BCM | Net | Wired to | Pull (configured) |
|---:|---|---:|---|---|---|
| 2 | 5V |  | 5V | C2.1 (Capacitor), C3.1 (Capacitor), C4.1 (Capacitor), C5.1 (Capacitor), H1.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H2.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H3.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H4.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H5.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H6.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H7.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H8.VCC (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), PS1.V+ (5 V 5 A DC adapter (5.5x2.1 mm barrel)), U11.+ (ULN2003 stepper driver board), U12.+ (ULN2003 stepper driver board), U13.+ (ULN2003 stepper driver board), U14.+ (ULN2003 stepper driver board), U15.+ (ULN2003 stepper driver board), U16.+ (ULN2003 stepper driver board), U17.+ (ULN2003 stepper driver board), U18.+ (ULN2003 stepper driver board), U2.SRCLR (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U2.VCC (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U3.SRCLR (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U3.VCC (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U4.SRCLR (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U4.VCC (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U5.SRCLR (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U5.VCC (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 4 | 5V |  | 5V | C1.1 (Capacitor) |  |
| 6 | GND |  | GND | C5.2 (Capacitor), H1.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H2.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H3.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H4.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H5.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H6.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H7.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), H8.GND (A3144 unipolar Hall-effect switch, open collector (TO-92UA)), PS1.GND (5 V 5 A DC adapter (5.5x2.1 mm barrel)), U11.- (ULN2003 stepper driver board), U12.- (ULN2003 stepper driver board), U13.- (ULN2003 stepper driver board), U14.- (ULN2003 stepper driver board), U15.- (ULN2003 stepper driver board), U16.- (ULN2003 stepper driver board), U17.- (ULN2003 stepper driver board), U18.- (ULN2003 stepper driver board), U5.OE (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U5.GND (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 7 | GPIO4 | 4 | N$15 | H1.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |
| 9 | GND |  | GND | C1.2 (Capacitor) |  |
| 14 | GND |  | GND | U2.OE (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U2.GND (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 19 | GPIO10 | 10 | N$1 | U2.SER (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 20 | GND |  | GND | C2.2 (Capacitor) |  |
| 23 | GPIO11 | 11 | N$2 | U2.SRCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U3.SRCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U4.SRCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U5.SRCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 24 | GPIO8 | 8 | N$3 | U2.RCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U3.RCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U4.RCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U5.RCLK (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 25 | GND |  | GND | U3.OE (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U3.GND (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 29 | GPIO5 | 5 | N$24 | H2.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |
| 30 | GND |  | GND | C3.2 (Capacitor) |  |
| 31 | GPIO6 | 6 | N$33 | H3.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |
| 33 | GPIO13 | 13 | N$42 | H4.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |
| 34 | GND |  | GND | U4.OE (74HCT595 8-bit shift register, TTL inputs (DIP-16)), U4.GND (74HCT595 8-bit shift register, TTL inputs (DIP-16)) |  |
| 35 | GPIO19 | 19 | N$60 | H6.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |
| 36 | GPIO16 | 16 | N$51 | H5.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |
| 38 | GPIO20 | 20 | N$69 | H7.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |
| 39 | GND |  | GND | C4.2 (Capacitor) |  |
| 40 | GPIO21 | 21 | N$78 | H8.OUT (A3144 unipolar Hall-effect switch, open collector (TO-92UA)) | pull-up |

Interfaces enabled: spi0 (see config.txt).

## 40-pin header (top view, pin 1 = 3V3)

| Wired to | Pin | # | # | Pin | Wired to |
|---|---|---:|---:|---|---|
|  | 3V3 | 1 | 2 | 5V | C2.1, C3.1, C4.1, C5.1, H1.VCC, H2.VCC, H3.VCC, H4.VCC, H5.VCC, H6.VCC, H7.VCC, H8.VCC, PS1.V+, U11.+, U12.+, U13.+, U14.+, U15.+, U16.+, U17.+, U18.+, U2.SRCLR, U2.VCC, U3.SRCLR, U3.VCC, U4.SRCLR, U4.VCC, U5.SRCLR, U5.VCC |
|  | GPIO2 | 3 | 4 | 5V | C1.1 |
|  | GPIO3 | 5 | 6 | GND | C5.2, H1.GND, H2.GND, H3.GND, H4.GND, H5.GND, H6.GND, H7.GND, H8.GND, PS1.GND, U11.-, U12.-, U13.-, U14.-, U15.-, U16.-, U17.-, U18.-, U5.OE, U5.GND |
| H1.OUT | GPIO4 | 7 | 8 | GPIO14 |  |
| C1.2 | GND | 9 | 10 | GPIO15 |  |
|  | GPIO17 | 11 | 12 | GPIO18 |  |
|  | GPIO27 | 13 | 14 | GND | U2.OE, U2.GND |
|  | GPIO22 | 15 | 16 | GPIO23 |  |
|  | 3V3 | 17 | 18 | GPIO24 |  |
| U2.SER | GPIO10 | 19 | 20 | GND | C2.2 |
|  | GPIO9 | 21 | 22 | GPIO25 |  |
| U2.SRCLK, U3.SRCLK, U4.SRCLK, U5.SRCLK | GPIO11 | 23 | 24 | GPIO8 | U2.RCLK, U3.RCLK, U4.RCLK, U5.RCLK |
| U3.OE, U3.GND | GND | 25 | 26 | GPIO7 |  |
|  | GPIO0 | 27 | 28 | GPIO1 |  |
| H2.OUT | GPIO5 | 29 | 30 | GND | C3.2 |
| H3.OUT | GPIO6 | 31 | 32 | GPIO12 |  |
| H4.OUT | GPIO13 | 33 | 34 | GND | U4.OE, U4.GND |
| H6.OUT | GPIO19 | 35 | 36 | GPIO16 | H5.OUT |
|  | GPIO26 | 37 | 38 | GPIO20 | H7.OUT |
| C4.2 | GND | 39 | 40 | GPIO21 | H8.OUT |
