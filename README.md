# Bluetooth-Transmitter-with-Ghost-Recording
A USB-C Bluetooth audio transmitter that lets you keep using your own headphones while silently capturing both sides of any meeting.

Plug the dongle into your computer or phone during Zoom/Teams (or any VoIP) calls. It appears as a normal Bluetooth transmitter:

- Remote / system audio is played cleanly into your personal earbuds
- Your voice is sent back to the computer so you can speak normally
- At the same time, both the transmit (your voice) and receive (remote participants) audio streams are recorded locally in high quality — completely silently, with no bot, no speaker output, and no interruption to the call

**Key features**
- Works with any standard Bluetooth headphones (no proprietary earbuds required)
- True bi-directional “Ghost Recording” of Tx + Rx audio
- Qualcomm QCC5181 Bluetooth SoC + Winbond W25N01G NAND flash for local storage
- Low-latency, clear voice capture
- One-click export of the recorded audio

Open-source hardware & firmware focused on private, local meeting recording without changing how you already work.

PIO configuration

#define W25N01G_PIO_SPI_CS          PIO1

#define W25N01G_PIO_SPI_MISO        PIO2

#define W25N01G_PIO_SPI_MOSI        PIO3

#define W25N01G_PIO_SPI_CLK         PIO4
