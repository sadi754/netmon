<?php
// NetMon relay - first-time setup in the browser (no Python, no FTP client needed).
//
// Upload the relay's files (e.g. with cPanel's File Manager), then open
// https://your-relay/setup.php once: it checks the host, makes the two tokens and
// writes config.php. After that it refuses to run again - delete config.php to
// start over (new tokens), and you may delete this file.

const CONFIG = __DIR__ . '/config.php';
const DATA = __DIR__ . '/data';

function token() {
  return rtrim(strtr(base64_encode(random_bytes(32)), '+/', '-_'), '=');
}

function h($s) {
  return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8');
}

$https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
      || (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https');
$base = ($https ? 'https://' : 'http://') . (isset($_SERVER['HTTP_HOST']) ? $_SERVER['HTTP_HOST'] : 'your-relay')
      . rtrim(str_replace('\\', '/', dirname(isset($_SERVER['SCRIPT_NAME']) ? $_SERVER['SCRIPT_NAME'] : '/')), '/');

$checks = [
  ['PHP 7.3 or newer', version_compare(PHP_VERSION, '7.3.0', '>='), 'This host runs PHP ' . PHP_VERSION . '.'],
  ['SQLite for PHP (pdo_sqlite)', extension_loaded('pdo_sqlite'), 'Turn on the pdo_sqlite extension in the host\'s PHP settings.'],
  ['The data folder can be written', is_dir(DATA) && is_writable(DATA), 'Create a folder named "data" next to this file and make it writable (permissions 755 or 775).'],
  ['This folder can be written (for config.php)', is_writable(__DIR__), 'Make the relay folder writable, or create config.php by hand.'],
  ['HTTPS', $https, 'Open this page with https:// - turn on an SSL certificate for the (sub)domain first (e.g. Let\'s Encrypt in cPanel).'],
];
$ready = !in_array(false, array_map(function ($c) { return $c[1]; }, array_slice($checks, 0, 4)), true);

$state = file_exists(CONFIG) ? 'done' : 'ask';
$ingest = $read = '';
if ($state === 'ask' && $_SERVER['REQUEST_METHOD'] === 'POST' && $ready && isset($_POST['go'])) {
  $ingest = token();
  $read = token();
  $config = "<?php\n// Made by setup.php - not served (see .htaccess). Delete it to set the relay up again.\n"
          . "define('INGEST_TOKEN', " . var_export($ingest, true) . ");\n"
          . "define('READ_TOKEN', " . var_export($read, true) . ");\n"
          . "define('DATA_DIR', __DIR__ . '/data');\n";
  // x: fails if config.php appeared meanwhile (two people setting up at once)
  $f = @fopen(CONFIG, 'x');
  if ($f && fwrite($f, $config) === strlen($config)) {
    fclose($f);
    @chmod(CONFIG, 0640);
    $state = 'made';
  } else {
    if ($f) { fclose($f); @unlink(CONFIG); }
    $state = file_exists(CONFIG) ? 'done' : 'failed';
  }
}
header('Cache-Control: no-store');
header('X-Robots-Tag: noindex');
?><!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>NetMon relay setup</title>
<style>
  body { font: 15px/1.55 system-ui, "Segoe UI", sans-serif; background: #0a0e14; color: #a7b2c3; margin: 0; padding: 40px 16px; }
  main { max-width: 760px; margin: 0 auto; }
  h1 { color: #e6edf6; font-weight: 600; margin: 0 0 6px; }
  h2 { color: #e6edf6; font-size: 17px; margin: 26px 0 8px; }
  .box { background: #111823; border: 1px solid #222c3b; border-radius: 10px; padding: 16px 18px; margin: 14px 0; }
  .ok { color: #3fb950; } .no { color: #f85149; } .warn { color: #d29922; }
  code, .token { font-family: Consolas, "Cascadia Mono", monospace; }
  .token { display: block; background: #0a0e14; border: 1px solid #2a3547; border-radius: 6px; padding: 10px 12px; color: #e6edf6;
           word-break: break-all; user-select: all; margin: 6px 0 2px; font-size: 14px; }
  button { background: #1f6feb; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px; font: inherit; cursor: pointer; }
  button[disabled] { background: #30363d; color: #8b949e; cursor: default; }
  li { margin: 4px 0; }
</style>
</head>
<body><main>
<h1>NetMon relay setup</h1>
<p>Sets up this relay for <a href="https://github.com/sadi754/netmon" style="color:#58a6ff">NetMon</a>: your MikroTik router
  reports its internet byte counters here every minute, and NetMon reads them back.</p>

<?php if ($state === 'done'): ?>
  <div class="box"><b class="ok">This relay is already set up.</b>
    <p>The tokens were shown once, when it was set up. Lost them? Delete <code>config.php</code> in the relay's folder
      (cPanel → File Manager) and open this page again to make new ones - then update NetMon and the router script.
      The recorded traffic stays.</p>
    <p>You may delete <code>setup.php</code> now.</p></div>

<?php elseif ($state === 'made'): ?>
  <div class="box"><b class="ok">Done - the relay is ready.</b> Copy the two tokens now: they are shown only this once.</div>
  <h2>1. In NetMon</h2>
  <div class="box">Router tab → <b>Through a relay</b>:
    <ul>
      <li>Relay address: <span class="token"><?= h($base) ?></span></li>
      <li>Read token: <span class="token"><?= h($read) ?></span></li>
    </ul>Then press <b>Save</b> and <b>Test</b>.</div>
  <h2>2. The router script</h2>
  <div class="box">In the same place, open <b>Router script</b>, enter your internet interface (e.g. <code>pppoe-out1</code>),
    optionally your domestic address list, and this <b>router token</b>:
    <span class="token"><?= h($ingest) ?></span>
    Press <b>Make script</b> and paste it into the router's terminal (Winbox → New Terminal).</div>
  <h2>3. Tidy up</h2>
  <div class="box">Delete <code>setup.php</code> from the relay's folder - it won't run again anyway.</div>

<?php else: ?>
  <h2>Check</h2>
  <div class="box"><ul style="list-style:none; padding:0; margin:0">
  <?php foreach ($checks as $i => $c): ?>
    <li><?= $c[1] ? '<span class="ok">✔</span>' : ($i === 4 ? '<span class="warn">!</span>' : '<span class="no">✘</span>') ?>
      <?= h($c[0]) ?><?= $c[1] ? '' : ' – <span class="' . ($i === 4 ? 'warn' : 'no') . '">' . h($c[2]) . '</span>' ?></li>
  <?php endforeach; ?>
  </ul></div>
  <?php if ($state === 'failed'): ?>
    <div class="box no">Couldn't write config.php. Check the folder's permissions and try again.</div>
  <?php endif; ?>
  <form method="post">
    <p>This makes two random tokens - one for the router (to send), one for NetMon (to read) - and saves them in
      <code>config.php</code>, which the web server never shows to anyone.</p>
    <button name="go" value="1" <?= $ready ? '' : 'disabled' ?>>Set up the relay</button>
  </form>
<?php endif; ?>
</main></body>
</html>
