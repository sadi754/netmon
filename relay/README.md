# NetMon relay (optional)

**English** · [فارسی](README.fa.md)

NetMon itself only sees the PC it runs on. If your internet comes through a
**MikroTik router (RouterOS 7)**, the relay lets NetMon show the usage of the
**whole network** – every phone, TV and PC – including the hours when your PC
is switched off.

```
MikroTik ──(every minute: WAN byte counters)──▶ relay (PHP + SQLite) ◀──(when it runs)── NetMon
```

- The router sends only **byte counters** of its internet interface (and,
  optionally, the domestic part of them). No addresses, sites or devices.
- It sends the router's **cumulative** counters, so a missed minute loses
  nothing; restarts (router reboot, PPPoE reconnect) are detected by the relay.
- Any PHP 7.3+ host with SQLite will do – a small shared hosting plan is plenty
  (one tiny request per minute).

## Set up

1. **Host.** Create a (sub)domain with HTTPS, e.g. `netmon.example.com`, and an
   FTP account limited to its folder (cPanel → FTP Accounts → Directory).
2. **Credentials file**, outside the project: `~/.netmon-deploy.env`
   ```
   FTP_HOST=your-host
   FTP_USER=netmon@example.com
   FTP_PASS=...
   SITE_URL=https://netmon.example.com
   ```
3. **Tokens and upload:**
   ```
   python relay/deploy.py --init       # adds INGEST_TOKEN and READ_TOKEN to the file
   python relay/deploy.py              # uploads the relay
   ```
4. **Router script:**
   ```
   python relay/deploy.py --router-script --wan pppoe-out1 --list IP-IR
   ```
   writes `~/netmon-router-feed.rsc` (NetMon's Router tab can make it too).
   Import it on the router (`/import file-name=netmon-router-feed.rsc`) or paste it.
   `--list` is optional: the name of an address list holding your country's IP
   ranges. Four passthrough mangle rules then count domestic and all traffic; the
   domestic share is their ratio, applied to the interface's exact counters, so a
   queue dropping packets or IPsec (counted once, via `ipsec-policy=...,none`)
   doesn't skew it. If FastTrack is on, fast-tracked traffic skips mangle.

   **Where to get your country's IP list:** for Iran, the address list can be made with
   [mo13ammad/Country-IP-List](https://github.com/mo13ammad/Country-IP-List), which builds
   a MikroTik address list of a country's IP ranges (that's how the author's own `IP-IR`
   list was made). Any address list with your country's ranges works; keep it up to date.
5. **Check:** `python relay/deploy.py --status` shows what has arrived.
6. **NetMon:** Settings → Integrations → MikroTik router, then in the Router tab
   enter the relay address and the `READ_TOKEN`.

## API

| | |
|---|---|
| `POST /api/ingest` | router → relay. `Authorization: Bearer INGEST_TOKEN`. JSON: `router`, `iface`, `uptime` (`1w2d03:04:05`), cumulative `rx`, `tx` (interface) and `drx`, `dtx`, `arx`, `atx` (domestic / all, from the counting rules), in bytes |
| `GET /api/traffic?after=ID&limit=N` | NetMon reads new rows. `Authorization: Bearer READ_TOKEN`. Each row is the traffic between two readings: `id, router, iface, ts, span, rx, tx, drx, dtx, arx, atx, reset` |
| `GET /api/status` | last reading per router interface, row count, database size |
| `GET /api/health` | no token – is the relay up? |

The database is `public/data/netmon.sqlite` on the host; `.htaccess` never
serves it or `config.php` (which `deploy.py` builds from your tokens).
