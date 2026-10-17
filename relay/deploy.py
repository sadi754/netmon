"""Upload the NetMon relay to a PHP host, and make the matching router script.

    python relay/deploy.py --init       create the two tokens (once)
    python relay/deploy.py              upload the relay (config.php is made from the tokens)
    python relay/deploy.py --status     show what the relay has received
    python relay/deploy.py --router-script [--wan pppoe-out1] [--list IP-IR]
                                        write the RouterOS script, filled in, next to the env file

Credentials and tokens live OUTSIDE the project, in ~/.netmon-deploy.env, so
sharing or zipping the project never carries them. One KEY=value per line:
    FTP_HOST=...        the host (IP or name)
    FTP_USER=...        an FTP account limited to the relay's folder
    FTP_PASS=...           (cPanel -> FTP Accounts -> Directory = the subdomain's folder)
    SITE_URL=https://netmon.example.com
    INGEST_TOKEN=...    written by --init: the router sends this one
    READ_TOKEN=...      written by --init: NetMon reads with this one
"""
import ftplib
import io
import json
import os
import secrets
import ssl
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")
ENV_FILE = os.path.join(os.path.expanduser("~"), ".netmon-deploy.env")
SITE_FILES = [".htaccess", "api.php", "index.html", "data/.htaccess"]


def load_env() -> dict:
    if not os.path.exists(ENV_FILE):
        sys.exit(f"no {ENV_FILE} - see the top of relay/deploy.py")
    env = {}
    with open(ENV_FILE, encoding="utf8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def init_tokens() -> None:
    env = load_env() if os.path.exists(ENV_FILE) else {}
    new = {k: secrets.token_urlsafe(32) for k in ("INGEST_TOKEN", "READ_TOKEN") if not env.get(k)}
    if not new:
        print("tokens already exist - nothing to do")
        return
    with open(ENV_FILE, "a", encoding="utf8") as f:
        f.write("\n# NetMon relay tokens (made by relay/deploy.py --init)\n")
        for key, value in new.items():
            f.write(f"{key}={value}\n")
    print(f"added {', '.join(new)} to {ENV_FILE}")


def config_php(env: dict) -> bytes:
    for key in ("INGEST_TOKEN", "READ_TOKEN"):
        if len(env.get(key, "")) < 16:
            sys.exit(f"{key} missing - run: python relay/deploy.py --init")
    return (
        "<?php\n// Made by relay/deploy.py from ~/.netmon-deploy.env - not served (see .htaccess).\n"
        f"define('INGEST_TOKEN', {json.dumps(env['INGEST_TOKEN'])});\n"
        f"define('READ_TOKEN', {json.dumps(env['READ_TOKEN'])});\n"
        "define('DATA_DIR', __DIR__ . '/data');\n"
    ).encode()


def connect(env: dict) -> ftplib.FTP_TLS:
    # Explicit FTPS: the password and files travel encrypted. The certificate is
    # not checked because we may connect by IP while it is issued for a name.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ftp = ftplib.FTP_TLS(env["FTP_HOST"], timeout=60, context=ctx)
    ftp.login(env["FTP_USER"], env["FTP_PASS"])
    ftp.prot_p()
    return ftp


def upload(ftp: ftplib.FTP_TLS, data: bytes, remote: str) -> None:
    folder = os.path.dirname(remote)
    if folder:
        try:
            ftp.mkd(folder)
        except ftplib.error_perm:
            pass  # already there
    ftp.storbinary("STOR " + remote, io.BytesIO(data))
    print(f"  uploaded {remote:16} {len(data):>7} bytes")


def deploy(env: dict) -> None:
    config = config_php(env)
    ftp = connect(env)
    print(f"connected to {env['FTP_HOST']}")
    for name in SITE_FILES:
        with open(os.path.join(PUBLIC, name), "rb") as f:
            upload(ftp, f.read(), name)
    upload(ftp, config, "config.php")
    ftp.quit()
    print("done")


def status(env: dict) -> None:
    req = urllib.request.Request(env["SITE_URL"].rstrip("/") + "/api/status",
                                 headers={"Authorization": "Bearer " + env["READ_TOKEN"]})
    print(json.dumps(json.loads(urllib.request.urlopen(req, timeout=30).read()), indent=2))


def router_script(env: dict, wan: str, address_list: str) -> None:
    sys.path.insert(0, os.path.dirname(HERE))  # the same script builder the NetMon app uses
    from netmon import routerscript
    try:
        text = routerscript.render(env["SITE_URL"], env["INGEST_TOKEN"], wan, address_list)
    except ValueError as e:
        sys.exit(str(e))
    out = os.path.join(os.path.dirname(ENV_FILE), "netmon-router-feed.rsc")
    with open(out, "w", encoding="utf8", newline="\r\n") as f:
        f.write(text)
    print(f"wrote {out} - it contains the router's token, so keep it private")


def main(args: list[str]) -> None:
    if "--init" in args:
        init_tokens()
        return
    env = load_env()
    if "--status" in args:
        status(env)
    elif "--router-script" in args:
        option = lambda name, default: args[args.index(name) + 1] if name in args else default  # noqa: E731
        router_script(env, option("--wan", "pppoe-out1"), option("--list", ""))
    else:
        deploy(env)


if __name__ == "__main__":
    main(sys.argv[1:])
