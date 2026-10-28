# MikroTik router: the whole network's usage (optional)

**English** · [فارسی](README.fa.md)

NetMon on its own sees only the PC it runs on. With a **MikroTik router (RouterOS 7)**
it can also show the internet usage of the **whole network** – every phone, TV and PC –
split into domestic and international traffic, and count your data quota from it.

Only **byte counters** of the router's internet interface are read – no addresses, no
sites, no devices. Turn it on in **Settings → Integrations → MikroTik router**; a
**Router** tab appears. Then pick one of three ways:

| | How | Needs | While this PC is off |
|---|---|---|---|
| **1. Direct** | NetMon reads the router every minute | RouterOS 7.1+, nothing else | the router keeps counting; NetMon gets it later (shown as an estimate) – **a router restart in that time loses the traffic since the last reading** |
| **2. PHP relay** | the router reports every minute to a small PHP program on a web host | any PHP host with SQLite (shared hosting is fine) | recorded minute by minute |
| **3. Cloudflare relay** | the same, on Cloudflare Workers + D1 | a free Cloudflare account | recorded minute by minute |

Direct is the easiest. A relay never misses anything, even when the router restarts
while your PC is off.

## The domestic share (optional, all three ways)

To split domestic and international traffic, the router needs an **address list** with
your country's IP ranges. NetMon's scripts then add four "passthrough" mangle rules that
only count (nothing is changed): domestic and all traffic, in and out. The domestic share
is their ratio, applied to the interface's exact counters – so a queue dropping packets,
or IPsec (counted once, via `ipsec-policy=…,none`), doesn't skew it.

- **Iran:** [mo13ammad/Country-IP-List](https://github.com/mo13ammad/Country-IP-List)
  makes a MikroTik address list of a country's IP ranges (that's how the author's own
  `IP-IR` list was made). Any list with your country's ranges works; keep it up to date.
- If **FastTrack** is on, fast-tracked connections skip mangle, so the domestic share
  reads too low (the totals stay right). The scripts warn about it.

---

## 1. Direct (no server)

1. Router tab → **Direct – no server**.
2. Enter the router's address (its IP in your local network, e.g. `192.168.88.1`), the
   internet interface (e.g. `pppoe-out1`) and, optionally, the domestic address list.
3. **Make setup script**, copy it, and paste it into the router's terminal
   (Winbox → New Terminal). It:
   - makes a user group `netmon-read` with only `read,web,rest-api` (it can't change
     anything) and a user `netmon` in it, with a random password, allowed to log in from
     your local network only. NetMon keeps the password encrypted for your Windows user.
   - turns on the router's `www` service (http, port 80) for your local network – only if
     it was off; if it's on, it is left alone (it must allow your PC's address).
   - adds the four counting rules, if you gave a domestic list.
4. Press **Test**. From then on NetMon reads the router once a minute while it runs.

**While NetMon isn't running** (PC off, NetMon closed), the router keeps counting. At the
next reading NetMon gets all of it and shares it out evenly over those hours – pale bars
in the chart, marked "estimated". **If the router restarts in that time, its counters
start again from zero, and the traffic between NetMon's last reading and the restart is
lost.** NetMon notices the restart and says which hours are unknown. If that matters to
you, use a relay.

<details><summary>Remove it from the router</summary>

```
/user remove [find name="netmon"]
/user group remove [find name="netmon-read"]
```
and the counting rules – only if no relay script uses them too:
```
/ip firewall mangle remove [find comment~"^netmon:"]
```
</details>

---

## 2. PHP relay (your own web host)

Any PHP 7.3+ host with SQLite – a small shared hosting plan is plenty (one tiny request
a minute).

1. **A (sub)domain with HTTPS**, e.g. `netmon.example.com` (cPanel → Domains; turn on a
   free Let's Encrypt certificate).
2. Download **`NetMon-relay-<version>.zip`** from the
   [Releases](https://github.com/sadi754/netmon/releases) page and upload its contents to
   the (sub)domain's folder (cPanel → File Manager → Upload, then Extract). Keep the
   `data` folder and the `.htaccess` files.
3. Open **`https://netmon.example.com/setup.php`** in a browser. It checks the host
   (PHP version, SQLite, folder permissions, HTTPS), makes two tokens and shows them once:
   - the **read token** – for NetMon,
   - the **router token** – for the router script.
4. NetMon → Router tab → **Through a relay**: enter the relay's address and the read
   token → **Save** → **Test**.
5. Open **Router script**, enter the internet interface, optionally the domestic list,
   and the router token → **Make script** → paste it into the router's terminal. It
   installs a scheduler (`netmon-feed`) that reports every minute.
6. Delete `setup.php` (it won't run again anyway). Lost the tokens? Delete `config.php`
   and open `setup.php` again – the recorded traffic stays.

The database is `data/netmon.sqlite`; `.htaccess` never serves it or `config.php`.

<details><summary>From the source, with Python (for developers)</summary>

Instead of steps 2–3, `relay/deploy.py` uploads the relay over FTPS and makes the tokens.
Put the host's details in `~/.netmon-deploy.env` (outside the project):
```
FTP_HOST=your-host
FTP_USER=netmon@example.com
FTP_PASS=...
SITE_URL=https://netmon.example.com
```
then:
```
python relay/deploy.py --init       # adds INGEST_TOKEN and READ_TOKEN to the file
python relay/deploy.py              # uploads the relay
python relay/deploy.py --router-script --wan pppoe-out1 --list IP-IR
python relay/deploy.py --status     # what has arrived
```
</details>

---

## 3. Cloudflare relay (free, no host)

The same relay as a Cloudflare Worker with a D1 database. The free plan is plenty.

> **In Iran:** `*.workers.dev` is often filtered, and some Cloudflare addresses are
> disrupted, so the **router** may not reach the relay (your PC may, if it goes through a
> VPN). If your router has a VPN, send the router's own traffic to the relay through it –
> see [Router behind filtering](#router-behind-filtering-iran) below. Otherwise use
> direct reading or a PHP host in Iran.

**In the Cloudflare dashboard** (no tools needed – button names may differ slightly):

1. **Workers & Pages → Create → Create Worker** (start from "Hello World"), name it
   `netmon-relay`, **Deploy**.
2. **Edit code**, replace everything with the contents of
   [`relay/cloudflare/worker.js`](cloudflare/worker.js), **Deploy**.
3. **Storage & Databases → D1 → Create database**, name it `netmon-relay`.
4. Back in the Worker: **Settings → Bindings → Add → D1 database**, variable name **`DB`**,
   database `netmon-relay` → **Deploy**.
5. Open **`https://netmon-relay.<your-subdomain>.workers.dev/setup`** and press
   **Set up the relay**. It shows the read token and the router token once.
6. Continue with steps 4–5 of the PHP relay above (NetMon, then the router script).

Lost the tokens? In the D1 database's **Console** run
`DELETE FROM meta WHERE key LIKE '%_token'` and open `/setup` again.

**With Wrangler** instead: see [`relay/cloudflare/wrangler.toml`](cloudflare/wrangler.toml).

### Router behind filtering (Iran)

If the router reaches the relay only through your VPN, mark the router's **own** traffic
to the relay for the VPN's routing table (your PCs' traffic is untouched). Replace
`VPN` with your routing table's name and the address with your relay's:

```
/ip firewall address-list add list=netmon-relay address=netmon-relay.example.workers.dev comment="NetMon: relay via VPN"
/ip firewall mangle add chain=output dst-address-list=netmon-relay action=mark-routing new-routing-mark=VPN passthrough=no comment="NetMon: relay via VPN"
```

RouterOS keeps the address list's IPs up to date from the name. The VPN interface needs a
masquerade (src-nat) rule, as for your PCs. Check with
`/tool fetch url="https://…/api/health" output=user`. To undo:
`/ip firewall mangle remove [find comment="NetMon: relay via VPN"]` and
`/ip firewall address-list remove [find list="netmon-relay"]`.

---

## Relay API

Both relays answer the same way:

| | |
|---|---|
| `POST /api/ingest` | router → relay. `Authorization: Bearer <router token>`. JSON: `router`, `iface`, `uptime` (`1w2d03:04:05`), cumulative `rx`, `tx` (interface) and `drx`, `dtx`, `arx`, `atx` (domestic / all, from the counting rules), in bytes |
| `GET /api/traffic?after=ID&limit=N` | NetMon reads new rows. `Authorization: Bearer <read token>`. Each row is the traffic between two readings: `id, router, iface, ts, span, rx, tx, drx, dtx, arx, atx, reset` |
| `GET /api/status` | last reading per router interface, the last row id |
| `GET /api/health` | no token – is the relay up? |

The router sends **cumulative** counters, so a missed minute loses nothing: the next
report covers it. A router restart is detected (`reset`).
