# HM30 Link

Send and receive data over the **SIYI HM30** radio's UART datalink:
**STM32 ↔ PC** or **PC ↔ PC**. Text, commands, numbers (live graph), raw bytes and
files, with every packet confirmed and re-sent if the radio loses it.

```
STM32 or PC ─UART─► HM30 AIR unit  ~~~ 5.8 GHz ~~~  HM30 GROUND unit ─USB-C─► PC (HM30 Link app)
```

## Folders

| Folder | What |
|---|---|
| `pc/` | Desktop app (Python + Tkinter) and its tests |
| `firmware/` | STM32 HAL library (`link.c/.h`, `link_proto.c/.h`) |

## PC app

**Easiest: `HM30Link.exe`** — one file, no Python needed. Double-click it.
(Windows may warn about an unknown publisher: *More info → Run anyway*.)
Build it yourself with `pc\build_exe.bat` (result: `pc\dist\HM30Link.exe`).

**Or with Python 3.10+:**

```powershell
cd pc
pip install -r requirements.txt
python hm30_link_app.py            # real hardware
python hm30_link_app.py --demo     # fake STM32 data, no hardware
python hm30_link_app.py --pair     # two windows joined by a simulated radio (try PC-to-PC)
```

The app needs `hm30_link_app.py`, `protocol.py`, `link_core.py` (and the two icon files) from `pc/`.

What it does:

- **Chat** tab: message bubbles, ✔ when the other side confirmed; received photos show a preview
  (click it to open), every received file has an "open folder" link
- **Log** tab in plain words (✔ done, ⚠ warning, ✖ problem); tick *Technical details* for packet-level info.
  Radio noise is counted and explained once instead of filling the screen.
- **Transfers** tab: every file sent and received (time, size, speed, status) with *Open file* /
  *Open folder*; kept in `logs/transfers.csv`
- **Auto-reconnect**: if the USB adapter or ground unit disconnects, the app keeps trying (even if
  Windows gives it another COM number) and re-sends unfinished files when it is back
- **Notifications**: when the window is in the background, new messages and files make it beep,
  blink in the taskbar and show the unread count in the title (can be switched off)
- **Values** table and live **graph** for numbers the other side sends
- **Send**: Text, Command, Number (`speed=1.5, angle=-30`), Bytes (hex), Files, Plain text line
- **Files**: pick several at once; progress bar with speed and time left; text-like files are
  compressed automatically; the receiver checks a CRC-32 before saving
- **Received files and logs** go to a folder the receiver chooses on first start
  (*Change…*, *Open folder*); every session is also logged to `logs/session_….csv`
- Status bar: link quality, delay, packets received (damaged / lost) and sent (confirmed / re-sent / failed)

## HM30 settings (ground unit OLED menu)

- **Datalink Mode → Type-C** (the ground unit appears as a COM port on the PC)
- **Baud** = the same value as the other side (115200 or 230400)
- Approximate speed: 115200 → ~10 KB/s, 230400 → ~20 KB/s (more for compressible files)

## Wiring the air unit UART (GH1.25 4-pin)

| Air unit pin | Wire | Connect to |
|---|---|---|
| 1 NC | red | nothing |
| 2 GND | black | GND |
| 3 TX | yellow | RX of the STM32 (PA10) or of a 3.3 V USB-UART adapter |
| 4 RX | blue | TX of the STM32 (PA9) or of a 3.3 V USB-UART adapter |

Logic level is 3.3 V. For PC ↔ PC, the sending PC uses a USB-UART adapter (CP2102 / CH340, 3.3 V)
on this connector.

## STM32 firmware

1. Copy `firmware/link.c`, `link_proto.c` to `Core/Src` and `link.h`, `link_proto.h` to `Core/Inc`.
2. CubeMX: USART1 Asynchronous (same baud as the HM30), **NVIC → USART1 global interrupt: on**.
   Use the HSE crystal (72 MHz on an F103) for an exact baud rate.
3. In `main.c`:

```c
#include "link.h"
...
link_init(&huart1);                 // after MX_USART1_UART_Init()
...
link_send_value("battery", 11.7f);  // number → Values table + graph
link_send_text("motor started");    // text → Chat

link_frame_t f;                     // everything the PC sends
while (link_receive(&f)) {
    switch (f.type) {
    case LINK_TYPE_CMD:   /* (char *)f.data, e.g. "LED ON" */ break;
    case LINK_TYPE_TEXT:  /* (char *)f.data */                 break;
    case LINK_TYPE_VALUE: { const char *name; float v;
                            link_frame_value(&f, &name, &v); } break;
    case LINK_TYPE_BYTES: /* f.data[0 .. f.len-1] */           break;
    }
}
```

The library confirms every packet from the PC by itself (from the UART interrupt) and ignores
re-sent copies, so each packet reaches your code once. Sending is non-blocking.

## Protocol

```
0xAA 0x55 | type | seq | len | payload (≤ 240) | CRC-16/CCITT-FALSE (little-endian)
```

Types: TEXT 1, VALUE 2 (float32 + name), BYTES 3, CMD 4, ACK 5, HELLO 6, FILE_START 7,
FILE_DATA 8, FILE_END 9. See `pc/protocol.py` and `firmware/link_proto.h`.

## Tests

```powershell
cd pc
python -m unittest test_protocol test_link -v
```

`test_link` runs two link workers over a simulated radio with 10 % packet loss and checks that
messages arrive exactly once and files arrive identical.
