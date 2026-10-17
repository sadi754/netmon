# NetMon relay (اختیاری)

[English](README.md) · **فارسی**

NetMon به‌تنهایی فقط کامپیوتری را می‌بیند که رویش اجرا می‌شود. اگر اینترنت شما از یک
**روتر MikroTik (RouterOS 7)** می‌آید، relay این امکان را می‌دهد که NetMon مصرف **کل
شبکه** را نشان دهد: همه‌ی گوشی‌ها، تلویزیون‌ها و کامپیوترها، حتی ساعت‌هایی که کامپیوتر شما
خاموش است.

```
MikroTik ──(every minute: WAN byte counters)──▶ relay (PHP + SQLite) ◀──(when it runs)── NetMon
```

- روتر فقط **شمارنده‌های بایت** اینترفیس اینترنت را می‌فرستد (و در صورت تمایل، سهم داخلی
  آن‌ها را). هیچ آدرس، سایت یا دستگاهی فرستاده نمی‌شود.
- روتر شمارنده‌های **تجمعی** را می‌فرستد، پس اگر یک دقیقه از دست برود چیزی گم نمی‌شود؛ ری‌استارت
  روتر یا قطع و وصل PPPoE را relay تشخیص می‌دهد.
- هر هاست PHP 7.3 به بالا با SQLite کافی است؛ یک هاست اشتراکی کوچک هم کاملاً کافی است
  (دقیقه‌ای یک درخواست کوچک).

## راه‌اندازی

1. **هاست.** یک (زیر)دامنه با HTTPS بسازید، مثلاً `netmon.example.com`، و یک حساب FTP که
   فقط به پوشه‌ی آن دسترسی دارد (cPanel → FTP Accounts → Directory).
2. **فایل مشخصات**، بیرون از پوشه‌ی پروژه: `~/.netmon-deploy.env`
   ```
   FTP_HOST=your-host
   FTP_USER=netmon@example.com
   FTP_PASS=...
   SITE_URL=https://netmon.example.com
   ```
3. **ساخت توکن‌ها و آپلود:**
   ```
   python relay/deploy.py --init       # INGEST_TOKEN و READ_TOKEN را به فایل اضافه می‌کند
   python relay/deploy.py              # relay را آپلود می‌کند
   ```
4. **اسکریپت روتر:**
   ```
   python relay/deploy.py --router-script --wan pppoe-out1 --list IP-IR
   ```
   فایل `~/netmon-router-feed.rsc` را می‌سازد (تب Router در NetMon هم می‌تواند آن را بسازد).
   آن را روی روتر import کنید (`/import file-name=netmon-router-feed.rsc`) یا در ترمینال روتر
   paste کنید. `--list` اختیاری است: نام یک address list که محدوده‌های IP کشورتان در آن است.
   در این حالت چهار قانون mangle از نوع passthrough ترافیک داخلی و کل ترافیک را می‌شمارند؛
   سهم داخلی از نسبت این دو به دست می‌آید و روی شمارنده‌های دقیق اینترفیس اعمال می‌شود، پس
   queueای که بسته‌ها را دور می‌ریزد یا IPsec (که با `ipsec-policy=...,none` فقط یک بار
   شمرده می‌شود) نتیجه را خراب نمی‌کند. اگر FastTrack روشن باشد، ترافیک fast-track شده از
   mangle عبور نمی‌کند.

   **لیست IP کشور را از کجا بیاوریم:** برای ایران می‌توانید address list را با
   [mo13ammad/Country-IP-List](https://github.com/mo13ammad/Country-IP-List) بسازید که
   محدوده‌های IP یک کشور را به‌صورت address list میکروتیک درمی‌آورد (لیست `IP-IR` خود سازنده‌ی
   NetMon هم از همین‌جا ساخته شده). هر address list دیگری با محدوده‌های IP کشورتان هم کار
   می‌کند؛ فقط آن را به‌روز نگه دارید.
5. **بررسی:** دستور `python relay/deploy.py --status` نشان می‌دهد چه داده‌ای رسیده است.
6. **NetMon:** گزینه‌ی Settings → Integrations → MikroTik router را روشن کنید، بعد در تب
   Router آدرس relay و `READ_TOKEN` را وارد کنید.

## API

| | |
|---|---|
| `POST /api/ingest` | روتر ← relay. `Authorization: Bearer INGEST_TOKEN`. بدنه‌ی JSON: `router`، `iface`، `uptime` (`1w2d03:04:05`)، شمارنده‌های تجمعی `rx`، `tx` (اینترفیس) و `drx`، `dtx`، `arx`، `atx` (داخلی / کل، از قانون‌های شمارش)، به بایت |
| `GET /api/traffic?after=ID&limit=N` | NetMon ردیف‌های جدید را می‌خواند. `Authorization: Bearer READ_TOKEN`. هر ردیف ترافیک بین دو خواندن است: `id, router, iface, ts, span, rx, tx, drx, dtx, arx, atx, reset` |
| `GET /api/status` | آخرین خواندن هر اینترفیس روتر، تعداد ردیف‌ها، حجم دیتابیس |
| `GET /api/health` | بدون توکن: relay بالا است؟ |

دیتابیس در مسیر `public/data/netmon.sqlite` روی هاست است؛ فایل `.htaccess` هرگز آن را یا
`config.php` را (که `deploy.py` از توکن‌های شما می‌سازد) در دسترس قرار نمی‌دهد.
