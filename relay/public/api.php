<?php
// NetMon relay - a tiny always-on server between a MikroTik router and NetMon.
//
// The router posts its internet (WAN) byte counters here every minute, even
// while the PC is off; NetMon reads the traffic back whenever it runs.
// Runs on any PHP 7.3+ host with SQLite (shared hosting is fine).
//
//   POST /api/ingest   router -> relay   (Authorization: Bearer INGEST_TOKEN)
//   GET  /api/traffic  relay -> NetMon   (Authorization: Bearer READ_TOKEN)
//   GET  /api/status   relay -> NetMon   (Authorization: Bearer READ_TOKEN)
//   GET  /api/health   anyone: is the relay up?
//
// The router sends its cumulative counters (they only grow), not differences,
// so a missed post loses nothing: the next one covers the gap. Counters restart
// when the router reboots or the PPPoE link reconnects; that is detected here.

require __DIR__ . '/config.php';   // INGEST_TOKEN, READ_TOKEN, DATA_DIR

const SCHEMA_VERSION = 2;           // 2: arx/atx - all traffic as the router's counting rules see it
const KEEP_DAYS = 800;             // traffic rows older than this are pruned
const MAX_BODY = 4096;

function out($data, $code = 200) {
  http_response_code($code);
  header('Content-Type: application/json; charset=utf-8');
  header('Cache-Control: no-store');
  echo json_encode($data);
  exit;
}

function err($message, $code) {
  out(['error' => $message], $code);
}

function require_token($expected) {
  $header = isset($_SERVER['HTTP_AUTHORIZATION']) ? $_SERVER['HTTP_AUTHORIZATION']
          : (isset($_SERVER['REDIRECT_HTTP_AUTHORIZATION']) ? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] : '');
  $given = stripos($header, 'Bearer ') === 0 ? trim(substr($header, 7)) : '';
  if (strlen((string)$expected) < 16 || !hash_equals((string)$expected, $given)) err('unauthorized', 401);
}

function db() {
  static $pdo = null;
  if ($pdo) return $pdo;
  $pdo = new PDO('sqlite:' . DATA_DIR . '/netmon.sqlite', null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
  ]);
  $pdo->exec('PRAGMA busy_timeout = 5000');
  $version = (int)$pdo->query('PRAGMA user_version')->fetchColumn();
  if ($version < 1) {
    $pdo->exec("
      CREATE TABLE IF NOT EXISTS counters (       -- the last reading of each router interface
        router TEXT NOT NULL, iface TEXT NOT NULL,
        ts INTEGER NOT NULL, uptime INTEGER NOT NULL,
        rx INTEGER NOT NULL, tx INTEGER NOT NULL, drx INTEGER NOT NULL, dtx INTEGER NOT NULL,
        PRIMARY KEY (router, iface));
      CREATE TABLE IF NOT EXISTS traffic (        -- bytes moved between two readings
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        router TEXT NOT NULL, iface TEXT NOT NULL,
        ts INTEGER NOT NULL,                      -- end of the interval (server clock, unix seconds)
        span INTEGER NOT NULL,                    -- seconds since the previous reading
        rx INTEGER NOT NULL, tx INTEGER NOT NULL, -- internet download / upload
        drx INTEGER NOT NULL, dtx INTEGER NOT NULL, -- the domestic part of it (0 if not measured)
        reset INTEGER NOT NULL DEFAULT 0);        -- 1: the router's counters had restarted
      CREATE INDEX IF NOT EXISTS traffic_ts ON traffic (ts);
    ");
  }
  if ($version < 2) {
    // All traffic as the mangle counting rules see it: the domestic share is drx/arx,
    // which cancels anything that makes mangle and the interface counters disagree.
    foreach (['counters', 'traffic'] as $table) {
      $pdo->exec("ALTER TABLE $table ADD COLUMN arx INTEGER NOT NULL DEFAULT 0");
      $pdo->exec("ALTER TABLE $table ADD COLUMN atx INTEGER NOT NULL DEFAULT 0");
    }
  }
  if ($version < SCHEMA_VERSION) $pdo->exec('PRAGMA user_version = ' . SCHEMA_VERSION);
  return $pdo;
}

// RouterOS uptime looks like "3w2d10:20:30" (plain seconds are accepted too).
function parse_uptime($value) {
  if (is_int($value) || (is_string($value) && ctype_digit($value))) return (int)$value;
  if (!is_string($value) || !preg_match('/^(?:(\d+)w)?(?:(\d+)d)?(\d+):(\d\d):(\d\d)(?:\.\d+)?$/', $value, $m)) return null;
  return (int)$m[1] * 604800 + (int)$m[2] * 86400 + (int)$m[3] * 3600 + (int)$m[4] * 60 + (int)$m[5];
}

function counter($value) {
  if (is_int($value) && $value >= 0) return $value;
  if (is_string($value) && ctype_digit($value) && strlen($value) <= 18) return (int)$value;
  return null;
}

function name_field($value) {
  return is_string($value) && preg_match('/^[\w .:@()\-]{1,64}$/u', $value) ? $value : null;
}

function ingest() {
  require_token(INGEST_TOKEN);
  $raw = file_get_contents('php://input', false, null, 0, MAX_BODY + 1);
  if (strlen($raw) > MAX_BODY) err('body too large', 413);
  $in = json_decode($raw, true);
  if (!is_array($in)) err('expected a JSON object', 400);

  $router = name_field(isset($in['router']) ? $in['router'] : 'router');
  $iface = name_field(isset($in['iface']) ? $in['iface'] : null);
  $uptime = parse_uptime(isset($in['uptime']) ? $in['uptime'] : null);
  $now = ['rx' => 0, 'tx' => 0, 'drx' => 0, 'dtx' => 0, 'arx' => 0, 'atx' => 0];
  foreach ($now as $key => $_) {
    $value = counter(isset($in[$key]) ? $in[$key] : 0);
    if ($value === null) err("bad value for $key", 400);
    $now[$key] = $value;
  }
  if ($router === null || $iface === null || $uptime === null) err('router, iface and uptime are required', 400);

  $pdo = db();
  $ts = time();
  $pdo->exec('BEGIN IMMEDIATE');
  $q = $pdo->prepare('SELECT * FROM counters WHERE router = ? AND iface = ?');
  $q->execute([$router, $iface]);
  $prev = $q->fetch();
  $stored = false;
  if ($prev) {
    $rebooted = $uptime < (int)$prev['uptime'];
    $delta = [];
    $reset = $rebooted;
    foreach ($now as $key => $value) {
      if ($rebooted || $value < (int)$prev[$key]) {  // the counter restarted from zero
        $delta[$key] = $value;
        $reset = true;
      } else {
        $delta[$key] = $value - (int)$prev[$key];
      }
    }
    if (array_sum($delta) > 0) {
      $pdo->prepare('INSERT INTO traffic (router, iface, ts, span, rx, tx, drx, dtx, arx, atx, reset)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
          ->execute([$router, $iface, $ts, max(1, $ts - (int)$prev['ts']), $delta['rx'], $delta['tx'],
                     $delta['drx'], $delta['dtx'], $delta['arx'], $delta['atx'], $reset ? 1 : 0]);
      $stored = true;
    }
  }
  $pdo->prepare('INSERT OR REPLACE INTO counters (router, iface, ts, uptime, rx, tx, drx, dtx, arx, atx)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
      ->execute([$router, $iface, $ts, $uptime, $now['rx'], $now['tx'], $now['drx'], $now['dtx'],
                 $now['arx'], $now['atx']]);
  $pdo->exec('COMMIT');
  if (mt_rand(1, 500) === 1) {  // now and then: drop very old rows
    $pdo->prepare('DELETE FROM traffic WHERE ts < ?')->execute([$ts - KEEP_DAYS * 86400]);
  }
  out(['ok' => true, 'stored' => $stored, 'first' => !$prev]);
}

function traffic() {
  require_token(READ_TOKEN);
  $after = isset($_GET['after']) && ctype_digit($_GET['after']) ? (int)$_GET['after'] : 0;
  $limit = isset($_GET['limit']) && ctype_digit($_GET['limit']) ? min(20000, max(1, (int)$_GET['limit'])) : 5000;
  $q = db()->prepare('SELECT id, router, iface, ts, span, rx, tx, drx, dtx, arx, atx, reset FROM traffic
                      WHERE id > ? ORDER BY id LIMIT ?');
  $q->execute([$after, $limit]);
  $rows = $q->fetchAll(PDO::FETCH_NUM);
  foreach ($rows as &$row) {
    foreach ([0, 3, 4, 5, 6, 7, 8, 9, 10, 11] as $i) $row[$i] = (int)$row[$i];
  }
  out(['fields' => ['id', 'router', 'iface', 'ts', 'span', 'rx', 'tx', 'drx', 'dtx', 'arx', 'atx', 'reset'],
       'rows' => $rows, 'more' => count($rows) === $limit, 'server_time' => time()]);
}

function status() {
  require_token(READ_TOKEN);
  $pdo = db();
  $routers = $pdo->query('SELECT router, iface, ts AS last_seen, uptime, rx, tx, drx, dtx, arx, atx FROM counters ORDER BY ts DESC')->fetchAll();
  $totals = $pdo->query('SELECT COUNT(*) AS rows, MIN(ts) AS first, MAX(id) AS last_id FROM traffic')->fetch();
  out(['routers' => $routers, 'rows' => (int)$totals['rows'], 'first' => $totals['first'] === null ? null : (int)$totals['first'],
       'last_id' => (int)$totals['last_id'], 'db_bytes' => @filesize(DATA_DIR . '/netmon.sqlite') ?: 0,
       'server_time' => time()]);
}

$path = parse_url(isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '/', PHP_URL_PATH);
$at = strpos($path, '/api/');
$route = $at === false ? '' : rtrim(substr($path, $at + 5), '/');
$method = isset($_SERVER['REQUEST_METHOD']) ? $_SERVER['REQUEST_METHOD'] : 'GET';

try {
  if ($route === 'ingest' && $method === 'POST') ingest();
  if ($route === 'traffic' && $method === 'GET') traffic();
  if ($route === 'status' && $method === 'GET') status();
  if ($route === 'health') out(['ok' => true, 'server_time' => time()]);
  err('not found', 404);
} catch (Exception $e) {
  error_log('netmon relay: ' . $e->getMessage());
  err('server error', 500);
}
