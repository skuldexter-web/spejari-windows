```
╔═╗╔═╗╔═╗ ╦╔═╗╦═╗╦
╚═╗╠═╝║╣  ║╠═╣╠╦╝║
╚═╝╩  ╚═╝╚╝╩ ╩╩╚═╩
ᛋ  scout of the 2.4 / 5 GHz airwaves  ·  Windows edition  ᛋ
```

# Spejari for Windows

**Spejari** (Old Norse *spæjari*, "scout") is a Wi-Fi war-driving tool for **Windows 10 / 11**. It drives **up to ten external Wi-Fi adapters at once**, tags every network with GPS coordinates from a **u-blox** receiver and, when you stop the session, writes a **WiGLE-format `.csv`** (compatible with WiGLE and wdgwars.pl-style uploads).

It comes with two interfaces that share one engine: a **terminal dashboard (CLI)** and a **desktop window (GUI, CustomTkinter)** with buttons and drop-down menus. Pick one at start-up or with `--cli` / `--gui`.

This is the Windows edition of Spejari. The Linux / Raspberry Pi edition lives in its own repository; see [Differences from the Linux edition](#differences-from-the-linux-edition).

- 2.4 GHz and 5 GHz only (6 GHz networks are ignored)
- Passive-by-design data: SSID, BSSID, RSSI, channel, security type. Nothing is connected to, attacked or decrypted
- No administrator rights needed, no drivers to patch, no monitor mode: it uses the **Windows Native Wifi API**
- Up to 10 adapters, hot-plug aware, scans **staggered in time** so results arrive continuously
- GPS from a u-blox COM port (auto-detected), or from a gpsd server
- Results are always saved as lowercase `.csv`; crash-safe journal with `--recover`
- `--simulate` runs everything without hardware (fake adapters and a simulated drive)

## Table of contents

1. [Quick start](#quick-start)
2. [How it works](#how-it-works)
3. [Differences from the Linux edition](#differences-from-the-linux-edition)
4. [Requirements and hardware](#requirements-and-hardware)
5. [GPS setup](#gps-setup)
6. [Usage](#usage)
7. [Configuration](#configuration)
8. [Output format and uploading](#output-format-and-uploading)
9. [Project layout](#project-layout)
10. [Troubleshooting](#troubleshooting)
11. [Development](#development)
12. [Publishing to GitHub](#publishing-to-github)
13. [Legal and ethics](#legal-and-ethics)

## Quick start

1. Install **Python 3.9+** from <https://www.python.org/downloads/windows/> (tick *Add python.exe to PATH*; keep *tcl/tk* selected for the GUI).
2. In the project folder, double-click **`setup.bat`** (creates `.venv` and installs the dependencies).
3. Turn on **Location access for desktop apps** (see [Troubleshooting](#troubleshooting): Windows requires it for Wi-Fi scanning).
4. Double-click **`spejari.bat`**, or from a terminal:

```bat
spejari.bat                 :: asks: [1] CLI  [2] GUI
spejari.bat --gui           :: desktop window
spejari.bat --cli           :: terminal dashboard (best in Windows Terminal)
spejari.bat --simulate      :: try it without hardware
spejari.bat --list-adapters :: which Wi-Fi adapters does Windows report?
spejari.bat --list-gps      :: which COM ports could be the GPS?
```

## How it works

### Scanning

Spejari talks to `wlanapi.dll` through `ctypes`. For every adapter it calls `WlanScan`, waits for the driver's sweep to finish (`scan_cycle_s`, default 4 s) and reads the BSS list with `WlanGetNetworkBssList`. That list contains BSSID, SSID, RSSI in dBm, the centre frequency and the raw information elements, from which Spejari decodes the security type itself (WPA, WPA2, WPA3-SAE, OWE, 802.1X/EAP, WEP, open). It does not depend on the Windows display language, unlike parsing `netsh` output.

### Multi-adapter clustering (up to 10)

Windows does **not** allow an application to choose which channels an adapter scans: a scan always sweeps every channel the driver supports. The Linux edition therefore cannot be copied one-to-one. Spejari for Windows distributes the adapters **in time** instead:

- every adapter runs its own scan loop in a worker thread
- the loops are **staggered**: with *N* adapters and a 4 s cycle you get a fresh scan result roughly every `4 / N` seconds (10 adapters: about every 0.4 s), which keeps coverage dense at driving speed
- adapters can be added or removed at any time (hot-plug): workers start and stop within about two seconds and the stagger is recomputed
- identical adapters (10 x the same dongle) get unique short **tags** (the end of the adapter GUID) so you can tell them apart; adapters can be switched on and off individually in the GUI or with `-i` / `-x`

### Stale results

Windows keeps a cache of recently seen networks. Spejari only accepts a network when its **beacon timestamp has changed** since the last read, so cached entries from a place you already left are not logged at your new position. The first read after pressing *Start* only primes this check (so you may lose up to one cycle at the very start).

### GPS and distance

A background thread reads NMEA from a u-blox COM port (or JSON from a gpsd server), tracks fix status (2D / 3D), satellites, HDOP, speed and the **cumulative distance** with jitter and glitch filtering. GPS time is used for the `FirstSeen` timestamps.

### Logging policy

A network is written to the CSV the first time it is seen **with a valid GPS fix**, again after you moved `min_distance_m` (25 m), or when its signal is `rssi_improvement_db` (8 dB) stronger than anything logged before. Networks seen without a fix appear in the live feed but are not written.

## Differences from the Linux edition

| | Linux / Raspberry Pi edition | Windows edition |
|---|---|---|
| Scan method | `iw dev <if> scan freq ...` | Windows Native Wifi API (`wlanapi.dll`) |
| Channel control | channels split across adapters | **not possible** - staggered scans instead |
| Passive scans | optional | not selectable (the driver decides) |
| Privileges | root | normal user, but Windows **location permission** required |
| GPS | gpsd or `/dev/ttyACM*` | COM port (pyserial) or gpsd over TCP |
| Launch | `./run.sh` | `spejari.bat` |

## Requirements and hardware

- Windows 10 or 11, Python 3.9+, the **WLAN AutoConfig** service running (default)
- **Wi-Fi adapters with Windows drivers**; each adapter must appear as a Wi-Fi interface in `spejari.bat --list-adapters`. Dual-band (2.4 + 5 GHz) adapters with external antennas work best
- For more than two adapters use **powered USB hubs** (each adapter can draw 300-500 mA; ten adapters need several amps). Spread them over more than one host port if you can, and keep the GPS puck away from the antennas
- A laptop with a good battery or a 12 V inverter for the car. Disable aggressive USB power saving (Device Manager > USB Root Hub > Power Management)
- A u-blox GPS receiver (u-blox 7 / 8 / 9 USB dongle or a module with USB-CDC), vendor id `1546`

Windows lists each adapter under the name its driver provides; Spejari shows that name next to a 4-character tag.

## GPS setup

Spejari needs the receiver as a **COM port**:

1. Plug in the dongle and open *Device Manager > Ports (COM & LPT)*. You should see something like *USB Serial Device (COM5)* or *u-blox GNSS receiver (COM5)*. Run `spejari.bat --list-gps` to confirm: u-blox devices are marked.
2. If the dongle shows up **only** under *Sensors* / *Location* (the "u-blox GNSS Location Sensor" driver) there is no COM port. Install the u-blox driver package that provides a COM port, or in Device Manager choose *Update driver > Browse my computer > Let me pick* and select **USB Serial Device**.
3. Start Spejari: with the default `--gps auto` it probes u-blox COM ports and reads NMEA. Pin a port with `--gps-device COM5` (or the GUI drop-down).
4. Place the receiver with a clear view of the sky. A cold start can take a few minutes.

A `gpsd` server (for example on a Raspberry Pi in the car) also works: `--gps gpsd --gpsd 192.168.1.50:2947`. Other COM devices are only probed when `probe_other_ports = true`, because opening an unrelated serial port can reset some microcontroller boards.

## Usage

| | CLI | GUI |
|---|---|---|
| Start | `spejari.bat --cli` | `spejari.bat --gui` |
| Best for | Windows Terminal, remote sessions | everyday use with a screen |
| Options | flags and `config\spejari.toml` | buttons, drop-down menus, switches |

Without a flag, Spejari asks which one you want (set `default_mode = "cli"` or `"gui"` in `[ui]` to skip the question).

### GUI

- **Start session / Stop & export CSV** (one toggle button)
- **Drop-downs:** record band (both / 2.4 only / 5 only), scan cycle (3 / 4 / 6 / 8 s), max adapters (1-10), GPS source (auto / serial / gpsd / none), GPS COM port, required fix quality (2D / 3D)
- **Switches:** ignore cached results, stagger adapters over time
- **Output:** *Browse...* chooses the export folder, *Open folder* opens it in Explorer
- **Adapter table:** one row per adapter with a **Use** checkbox; shows scans, hits, time since the last scan and state. Unticked adapters are shown as "excluded"; adapters beyond the limit as "over limit"
- **Cards:** session timer, unique SSIDs, BSSIDs, rows logged, distance (km), GPS status; live coordinates, altitude, speed and satellites underneath
- **Live feed:** newest sightings at the bottom (green = new, grey = update, red = open / WEP)
- Closing the window during a session asks to stop, export and quit

Band, cycle, max adapters, the switches and the adapter ticks apply live; GPS and output settings are locked while a session runs.

### CLI keys

| Key | Action |
|-----|--------|
| `S` | Start a session / stop it (compiles and exports the CSV) |
| `Q` or `Ctrl-C` | Quit; a running session is stopped and exported first |

The dashboard has a status panel (session, SSID counter, GPS fix, coordinates, **distance in km**), an adapter panel and a **live scrolling feed**. Use **Windows Terminal** for the best rendering.

### Command-line options

| Option | Description |
|--------|-------------|
| `--cli` / `--gui` | Choose the interface (default: ask) |
| `-c, --config FILE` | Use a specific TOML config |
| `-i, --interface X` | Use only adapters matching X: tag, GUID or part of the name (repeatable) |
| `-x, --exclude X` | Never use adapters matching X (repeatable) |
| `-n, --max-adapters N` | Adapter limit, 1-10 |
| `-b, --band {both,2.4,5}` | Record only this band |
| `--cycle SECONDS` | Scan cycle per adapter |
| `--gps {auto,serial,gpsd,none}` | GPS source |
| `--gps-device COMx` | GPS COM port (implies `--gps serial`) |
| `--gpsd HOST:PORT` | gpsd address |
| `-o, --output-dir DIR` | Export directory |
| `--auto-start` | Start scanning immediately |
| `--simulate [N]` | Simulated adapters and GPS drive, no hardware needed |
| `--list-adapters`, `--list-gps` | Show what Spejari can see, then exit |
| `--recover` | Convert journals of crashed sessions into CSV files |

## Configuration

All defaults live in [`config/spejari.toml`](config/spejari.toml), with comments. CLI flags override the file. Sections: `[scan]`, `[log]`, `[gps]`, `[ui]`.

## Output format and uploading

On stop, Spejari writes `exports\Spejari_YYYY-MM-DD_HH-MM-SS.csv` (session start, local time; the extension is always lowercase `.csv`). The format is **WigleWifi-1.4**:

```
WigleWifi-1.4,appRelease=Spejari 1.0.0,model=Windows 11,release=10.0.22631,device=MY-PC,display=,board=AMD64,brand=Spejari
MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type
AA:BB:CC:00:00:17,CoffeeShop,[WPA2-PSK-CCMP][ESS],2026-11-09 23:02:53,6,-63,52.22967700,21.01227800,110.4,4.5,WIFI
```

- `FirstSeen` is **UTC**, corrected with GPS time when available
- `AuthMode` uses the Android / WiGLE notation: `[WPA3-SAE-CCMP]`, `[WPA2-PSK-CCMP]`, `[WPA2-EAP-CCMP]`, `[WPA-PSK-TKIP]`, `[WEP]`, `[OWE-CCMP]`, `[ESS]` (open)
- Hidden networks are exported with an empty SSID

Upload the file at <https://wigle.net/uploads> or to wdgwars.pl. Spejari writes the standard WiGLE CSV such importers expect; check the site's upload page for its current rules and limits.

If Windows crashes or the power drops, run `spejari.bat --recover`: every logged sighting is flushed to `exports\.spejari_*.journal` as it happens and is turned back into a CSV.

## Project layout

```
spejariwindows/
├── main.py               entry point
├── spejari.bat           launcher (uses .venv if present)
├── setup.bat / .ps1      creates .venv and installs dependencies
├── build_exe.ps1         EXPERIMENTAL: single-file spejari.exe via PyInstaller
├── requirements.txt      rich, customtkinter, pyserial (+ tomli on Python < 3.11)
├── config/spejari.toml   documented defaults
├── spejari/
│   ├── cli.py            argument parsing, CLI/GUI chooser, --list-*, --recover
│   ├── app.py            controller shared by CLI and GUI: sessions, export, main loop
│   ├── wlan.py           wlanapi.dll wrapper (ctypes) and BSS list parsing
│   ├── security.py       WPA / WPA2 / WPA3 / OWE / WEP detection from beacon IEs
│   ├── scanner.py        per-adapter workers, staggering, hot-plug manager
│   ├── adapters.py       adapter detection, tags, include / exclude rules
│   ├── gps.py            COM-port NMEA reader, gpsd client, simulator, distance tracker
│   ├── session.py        de-duplication, logging policy, statistics
│   ├── exporter.py       WiGLE CSV writer, crash-safe journal, recovery
│   ├── ui.py             CLI: rich dashboard and keyboard handling (msvcrt)
│   ├── gui.py            GUI: CustomTkinter window
│   ├── sim.py            simulated adapters (with real information elements)
│   ├── channels.py       2.4 / 5 GHz helpers
│   ├── config.py, models.py, geo.py
└── tests/                pytest suite
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| **No networks appear / "access denied"** | Windows blocks Wi-Fi scans without location permission. *Settings > Privacy & security > Location*: turn on **Location services** and **Let desktop apps access your location**. Spejari prints a hint when scanning returns nothing |
| `No Wi-Fi adapter detected` | Wi-Fi switched off / airplane mode, or the **WLAN AutoConfig** service is stopped (`services.msc`). Check `--list-adapters` |
| An adapter is missing from the list | Install its Windows driver; it must appear under *Network adapters* as a wireless device |
| An adapter shows `error` or `paused` | Replug it; some drivers refuse scans while connecting. Excluding it with `-x` keeps the others running |
| Few results with 10 adapters | Use powered hubs; try a longer `--cycle 6` if drivers report incomplete sweeps |
| `No GPS COM port found` | See [GPS setup](#gps-setup): the dongle needs a COM port driver, then `--list-gps` / `--gps-device COMx` |
| GPS shows `NO FIX` | Move the antenna outdoors / near a window and wait for a cold start |
| Networks listed but nothing exported | There was no GPS fix while scanning: rows without coordinates are never written |
| GUI does not start | `pip install customtkinter` in the `.venv`, and make sure Python was installed with tcl/tk |
| Box characters or runes look wrong in the CLI | Use Windows Terminal (not the legacy console window) |

## Development

```bat
setup.bat -Dev
.venv\Scripts\python -m pytest -q
```

The tests cover the ctypes structure layouts (sizes and offsets checked against the Windows SDK values), BSS-list and adapter-list parsing on synthetic memory, security detection from raw information elements, NMEA parsing, distance filtering, adapter tagging and limits, hot-plug, stale-cache filtering, the CSV format and journal recovery. The scan pipeline is exercised end to end with `--simulate` (simulated adapters that emit real information elements). GitHub Actions runs the suite on Windows and Linux.

**Status:** the Windows API layer was developed against the SDK documentation and a simulated backend. Real-hardware reports and fixes are welcome: please open an issue with the output of `spejari.bat --list-adapters` and your Windows version.

## Publishing to GitHub

```bat
git init -b main
git add .
git commit -m "Initial commit: Spejari for Windows 1.0.0"

:: with the GitHub CLI
gh repo create spejariwindows --public --source=. --remote=origin --push

:: or: create an empty repo on github.com, then
git remote add origin https://github.com/YOUR-USER/spejariwindows.git
git push -u origin main
```

The GitHub web uploader does not unzip archives: extract the zip first and drag the **contents** of the folder into *Add file > Upload files*. The bundled `.gitignore` keeps your drive data (`exports\*.csv`, journals) out of the repository. **Never commit your wardriving logs**: they contain location data.

## Legal and ethics

Spejari only listens to the beacon and probe-response information that access points broadcast publicly; it does not connect to, attack, decrypt or interfere with any network. Laws on wireless collection and on recording location data differ by country and region: know yours, set the tool up before you drive and never operate it while driving, and publish data responsibly. Consider whether uploading your own home or your neighbours' networks to a public database is appropriate. You are responsible for how you use this software.

## License

MIT, see [LICENSE](LICENSE). A SKALDSEC project.
