# NetMon

**English** · [فارسی](README.fa.md)

A local, offline network monitor for Windows, in the spirit of NetWorx: live
bandwidth, usage history and data quotas, per-program traffic, a live connections
table with program names and geolocation, and a world map of where your traffic goes.

Everything runs on your computer. No accounts, no cloud, no telemetry. All
libraries, fonts and map data are bundled; the one optional download is the free
GeoIP database for the Connections tab (one click in Settings).

![NetMon: live speed, world map, connections and usage](docs/images/demo.gif)

## Features

- **Live speed** – download and upload, per second, for all physical adapters or
  one you pick, with a graph of the last minutes.
- **Tray icon** – a tiny live graph; hover for the speeds.
- **Mini graph and ping widget** – small always-on-top widgets that float on the
  desktop or **sit on the taskbar**. They hide during full-screen games and videos.
- **Usage history** – hours, days, weeks and months in the **Jalali (Shamsi)
  calendar**, as a chart or a table.
- **Ping history** – delay, loss and jitter to the internet and your router, always
  recorded in the background, with outages listed over the last hour to 30 days.
- **Data quota** – monthly, daily or every N days (prepaid packages), with
  warnings at 80% and 100%. Domestic traffic can count at a reduced rate.
- **Per-program traffic** – which program uses how much, right now and over time
  (with the free Npcap driver).
- **Connections** – every connection with the program's name and icon, host name,
  country, city and network (ISP), like TCPView with geolocation.
- **Map** – a flat world map or a 3D globe, with animated arcs to every place your
  PC is talking to.
- **VPN / proxy check** – shows a VPN adapter, a VPN program or Windows' system proxy.
- **Whole network (optional)** – with a MikroTik router: the usage of every device
  at home, split into domestic and international.
- **CSV export**, start with Windows, light on resources (about 50 MB in the
  background; the window's browser engine is freed when you close it).

| | |
|---|---|
| ![Usage](docs/images/usage.png) | ![Connections](docs/images/connections.png) |
| ![Live](docs/images/live.png) | ![Map](docs/images/map.png) |
| ![Ping history](docs/images/ping.png) | |

![Widgets: floating or on the taskbar](docs/images/widgets.png)

## Install

**Requirements:** Windows 10 or 11, 64-bit, with the Edge WebView2 runtime (built
into Windows 11; on Windows 10 it comes with Edge).

1. Download **`NetMon-Setup-<version>.exe`** from the
   [Releases](https://github.com/sadi754/netmon/releases) page and run it.
   No administrator rights are needed: NetMon installs for your user only, into
   `%LOCALAPPDATA%\Programs\NetMon`.
2. The installer isn't digitally signed, so Windows SmartScreen may say "Windows
   protected your PC". Click **More info → Run anyway**.
3. Choose the language (English or Persian), and optionally a desktop shortcut and
   **Start NetMon when I sign in**.

To update, run the new installer; your data and settings stay. To uninstall, use
Windows **Settings → Apps** – it asks whether to delete your data too.

**Portable (no installer):** unzip `NetMon-<version>-win64.zip` anywhere and run
`NetMon\NetMon.exe`. Create a folder named `data` next to `NetMon.exe` and NetMon
keeps everything in there (handy on a USB stick).

### Recommended: Npcap, for per-program traffic

[Npcap](https://npcap.com) is the free packet-capture driver Wireshark uses. Its
licence doesn't allow bundling it, so install it yourself. In its setup, leave
**"Restrict Npcap driver's access to Administrators only"** unticked, so NetMon
can capture without running as administrator. Without Npcap everything else still
works.

## First steps

1. NetMon opens its window and puts an icon in the tray. Windows 11 hides new tray
   icons behind the **^** arrow – drag the icon onto the taskbar to keep it visible.
2. **Settings → Connections → Home country**: pick your country. It decides what
   counts as "domestic" (automatic = the region set in Windows, which is often not
   where you are).
3. **Connections tab → Download** the free GeoIP database (about 65 MB, once) to
   see countries, cities and networks.
4. Optional: **Settings → Data quota** with your internet package's size.

Closing the window keeps NetMon running in the tray. Starting NetMon again while
it runs just brings the window back. Exit from the tray icon's menu.

## Guide

### Widgets: mini graph and ping

- **Mini graph** – the last minute of traffic and the current speeds.
  **Ping widget** – the live ping to a server you choose (for example your game
  server): the time, a quality dot (green under 60 ms, yellow under 120 ms, red
  above or lost), the last 30 pings, loss and jitter.
- **Drag** a widget anywhere; double-click it to open NetMon; right-click for
  **Opacity**, **Hide**, and (ping) **Change target…**. Show hidden widgets again
  from the tray menu.
- **On the taskbar:** drop a widget on the taskbar and it sits there in a compact
  form; drag it along to move it, drag it up to take it off again. Or right-click
  → **Move to taskbar / Move out of taskbar**. The text follows Windows' light or
  dark mode. (Not with an auto-hiding taskbar; main screen only.)
- While a game or full-screen video is in front, widgets on that screen hide
  themselves (Settings → Floating widgets).
- The ping widget sends one small ICMP echo per second, and only while it's shown.
  IPv4 only.

### Usage history and data quota

NetMon saves every adapter's traffic once a minute, as hourly totals, in a small
SQLite database (a year takes about 1 MB, plus a few MB for per-program history).
The **Usage** tab shows today, this week and this month, and a chart by hours,
days, weeks (starting Saturday) or months – in the Jalali calendar – or a table.

Turn on a **data quota** in Settings: per Jalali month, per day, or every N days
from a start date (prepaid packages), counting download + upload or download only.
The Usage tab then shows what's used and left, days left, how much you can use per
day, and where the current pace leads. Windows notifications appear at 80% and
100% (once per period); the tray tooltip shows the quota too.

On its own NetMon counts **this PC only** – other devices sharing the package
aren't included (see *Whole network* below). 1 GB = 1024 MB; some ISPs count 1000.
With per-program traffic on, traffic that stays in your local network isn't
counted, and domestic traffic can count at a reduced rate (e.g. 50%).

### Ping history

NetMon pings your internet target (default `8.8.8.8`) and your router once a second
in the background – also while the widget is hidden – and keeps single pings for 7
days, per-minute statistics and outages for good (about 10 MB of traffic a day). The
**Ping** tab shows delay, loss and jitter as a chart over the last hour, 24 hours, 7
or 30 days (a min – p95 band), and lists **outages** – 3 or more pings lost in a row,
lasting until the first reply comes back, with whether the router answered too (so
you can tell your own network apart from the internet). Turn recording off, change
the target or the router to ping, or delete the history, in **Settings → Ping**.

### Per-program traffic (Npcap)

With Npcap installed, NetMon captures the packets on the physical adapters and
tells which program each belongs to. Switch it off in Settings → Per-program traffic.

- **Live** page: a **Programs** list with each program's download and upload
  speed. Click one to see its connections.
- **Connections**: each connection's speed and data; UDP traffic (games, calls,
  YouTube's QUIC) with its real destination; and real site names – the name a
  program asked for (from the PC's own DNS answers and the TLS "hello"), such as
  `www.youtube.com` instead of reverse DNS's `...1e100.net`.
- **Usage**: a **Programs** table (today / this week / this month) and a
  **Domestic / Intl** split of this PC's traffic.

NetMon keeps only each packet's first 96 bytes (addresses, ports, size), in memory;
only per-program totals are saved. It costs about 1–3% of one CPU core (measured:
2.3% at 25 Mbit/s, no packets lost). Limits: a VPN or system proxy running *on the
PC* tunnels everything, so that traffic shows as the VPN/proxy program (a VPN on
the router doesn't have this problem). A connection that lasts a fraction of a
second may show as "Unknown program".

### Connections

Every TCP connection and open port on this PC, like `netstat -ano` or TCPView,
updated every 2 seconds:

- **Program** with its icon and name ("Google Chrome", not just `chrome.exe`);
  for `svchost.exe`, the Windows services inside (e.g. *Windows Update*).
- **Remote address** with host name (reverse DNS, can be switched off), port and
  what it's for, **location** (flag, country, city), **network** (e.g. *Google
  LLC*, *Cloudflare*, with its AS number), state and age.
- Green dot = **domestic**, purple = international (your home country from Settings).
- Search, country filter, **By program** grouping, sorting, **Pause**, a detail
  panel, **CSV** export, and **Show on map**.

Locations come from **DB-IP's free GeoIP database** (City Lite + ASN Lite). It's
downloaded on request (about 65 MB, 130 MB on disk) and can be updated monthly in
Settings → Connections. All lookups happen on your PC. NetMon reads the connection
table only while the tab is visible.

### Map

Where this PC's connections go, on a **2D** world map (the default) or a **3D**
globe: a glowing point at home and an arc to every city. Arcs carrying traffic are
brighter, with dashes moving the way the data flows – **cyan** towards home
(download), **orange** away from it (upload). The **Destinations** list ranks places
by traffic; click a place, dot or arc to see its programs and connections. Scroll
to zoom, drag to move. The map is drawn only while its tab is visible.

### Whole network: MikroTik router (optional)

With a MikroTik router (RouterOS 7), NetMon can show the internet usage of the
**whole network** – every device – split into domestic and international traffic, and
count the data quota from it. Only the router's WAN byte counters are read. Turn it on
in **Settings → Integrations → MikroTik router**, then choose how NetMon gets them:

- **Direct** – NetMon reads the router itself every minute (no server). Simplest; but if
  the router restarts while NetMon isn't running, the traffic since its last reading is
  lost. Hours NetMon missed are filled in as estimates (pale bars).
- **PHP relay** – the router reports every minute to a small program on any PHP host
  (upload a zip, open `setup.php`). Nothing is missed while your PC is off.
- **Cloudflare relay** – the same on Cloudflare Workers + D1, free (in Iran the router
  may need to reach it through a VPN).

Step by step: [relay/README.md](relay/README.md).

### Adapters and VPNs

The total counts **physical adapters only** (Ethernet, Wi-Fi). A VPN on the PC adds
a virtual adapter, and every byte then passes through both – adding them up would
count it twice. Virtual adapters are still listed; pick any single adapter in the
window's **Adapter** menu.

### Export and data

- **CSV** buttons on the Usage, Router and Connections pages save what's shown, for
  Excel. Settings → General → **Export all data** saves every recorded hour in exact
  bytes, as a zip of CSV files.
- Your data (settings, history, GeoIP files, logs) is in `%LOCALAPPDATA%\NetMon`
  (Settings → General → **Open folder**).

### Privacy

The window talks to NetMon through a small web server that listens on `127.0.0.1`
only and needs a random token created at each start, so neither other computers nor
websites in your browser can read it. NetMon sends something over the network only
for features you use:

- **Host names** (Connections, on by default): reverse-DNS lookups go to your normal
  DNS server. Switch off in Settings.
- **GeoIP database**: downloaded from `download.db-ip.com` when you click Download
  or Update. Addresses are never sent anywhere to be located.
- **Ping widget**: pings the target you set, while shown.
- **MikroTik router** (off by default): reads your router's byte counters – directly in your local
  network, or from your own relay.

## Troubleshooting

- **"Windows protected your PC"** – the installer and exe aren't signed:
  **More info → Run anyway**.
- **No Programs list / "Unknown program" everywhere** – install Npcap with
  *"Restrict … to Administrators only"* unticked, then restart NetMon.
- **The window is empty or white** – NetMon reloads it by itself within a few
  seconds. If it stays empty, close the window and open it again, and check
  `window.log` in the data folder.
- **The tray icon is gone** – Windows 11 hides it behind **^**; drag it to the taskbar.
- **"Move to taskbar" is greyed out** – the taskbar is set to auto-hide.
- **Router tab, direct: "not allowed" or "refused"** – run the setup script again (it sets the
  user's permissions and password); the router's `www` service must allow your PC's address.
- **Logs** – `netmon.log` and `window.log` in `%LOCALAPPDATA%\NetMon`
  (Settings → About → Open log file).

## Build from source

Needs Python 3.10 or newer (developed on 3.12).

```
run.bat                 start NetMon from the source
run.bat --minimized     start in the tray only
run.bat --console       live speeds in the terminal (Q quits)
run.bat --list          list network adapters
run.bat --debug         desktop app with log messages in the terminal
build.bat               build NetMon.exe, the zip and the installer
```

The first `run.bat` creates a private Python environment in `.venv` and installs
`requirements.txt` (your system Python isn't touched). From the source, data goes
to the `data` folder in the project.

`build.bat` installs PyInstaller, then makes `dist\NetMon\` (NetMon.exe and its
files), `dist\NetMon-<version>-win64.zip` and – if
[Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed
(`winget install JRSoftware.InnoSetup`) – `dist\NetMon-Setup-<version>.exe`.
The version is in `netmon/__init__.py`.

<details>
<summary>Project layout</summary>

```
run.bat, build.bat     start from the source / build
requirements*.txt      Python packages (run.bat installs them into .venv)
tools/                 PyInstaller recipe (netmon.spec), icon/version maker, zip maker
installer/             Inno Setup script (netmon.iss) and its Persian translation
netmon/                the app (Python)
  app.py               starts everything: monitor, web server, tray, widgets, window
  adapters.py          adapters and byte counters from Windows (GetIfTable2)
  speed.py, monitor.py per-second traffic; the last hour in memory
  history.py           usage history in SQLite
  usage.py, periods.py summaries, charts, quota; Jalali periods
  jalali.py            Jalali (Shamsi) calendar
  router.py            the whole network's traffic: from a NetMon relay, or directly (below)
  routerdirect.py      reads the router itself over its REST API; gaps as estimates
  routerscript.py      the MikroTik scripts: relay feed and direct-reading setup
  connections.py       TCP/UDP sockets and their owners (GetExtendedTcpTable)
  apps.py              program names, icons; services inside svchost.exe
  connwatch.py         the Connections tab's data, every 2 s while shown
  geoip.py             DB-IP databases: download, update, lookups
  rdns.py              host names (reverse DNS)
  vpn.py               VPN adapter / program / system proxy detection
  net.py               downloads that cope with filtered networks
  npcap.py             packet capture with Npcap (Packet.dll), in batches
  traffic.py           per-program and per-connection counting; LAN / domestic split
  names.py             site names from DNS answers and TLS client hellos
  export.py            CSV export
  autostart.py         Start with Windows (the per-user Run registry key)
  paths.py             where the program and the data are (source / exe / portable)
  server.py            local web server + WebSocket for the window
  tray.py              tray icon with live graph
  widget.py            floating widgets: native layered windows drawn with Pillow; taskbar docking
  minigraph.py         mini graph
  ping.py, pingwidget.py  ping (Windows ICMP API) and its widget
  pinghistory.py        the Ping tab's recorded history and outages
  window.py            the desktop window (Edge WebView2, separate process)
  icons.py             tray graph and app icon, drawn in code
  settings.py          settings.json
  console.py           terminal view
web/                   the window's UI (HTML/CSS/JS)
  vendor/              uPlot, globe.gl (with three.js), country flags, Natural Earth borders
relay/                 optional: the NetMon relay - PHP (public/, with setup.php) and Cloudflare
                       (cloudflare/worker.js) - and the router script templates
```
</details>

## Credits

- IP geolocation by [DB-IP](https://db-ip.com), licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (downloaded on request).
- [uPlot](https://github.com/leeoniya/uPlot) charts (MIT).
- 3D globe: [globe.gl](https://github.com/vasturiano/globe.gl) with
  [three.js](https://threejs.org) (MIT).
- Country flags: [country-flag-icons](https://gitlab.com/catamphetamine/country-flag-icons) (MIT).
- Country borders: [Natural Earth](https://www.naturalearthdata.com) (public domain).
- Packet capture: [Npcap](https://npcap.com) (installed separately).
- Iran IP ranges for the router's domestic list:
  [mo13ammad/Country-IP-List](https://github.com/mo13ammad/Country-IP-List).
- Python packages: Starlette, uvicorn, websockets, pywebview, pystray, Pillow, maxminddb.
- Installer: [Inno Setup](https://jrsoftware.org/isinfo.php), Persian translation by
  [Peyman Mohammadi](https://github.com/peymanr34/inno-setup-translations).

## License

[MIT](LICENSE) © 2026 sadi754
