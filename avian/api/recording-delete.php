<?php
// Aviary - authenticated false-positive removal for the detail modal.
//
// POST JSON: {"file":"<BirdNET recording basename>.mp3"}
//
// This endpoint always requires an authenticated admin session, even when LAN
// admin protection is otherwise disabled. A narrowly scoped root helper moves
// the audio/spectrogram into a private quarantine, takes a SQLite backup, and
// removes exactly one matching database row.

declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

require_once __DIR__ . '/admin-auth.php';

function respond(int $status, array $body): never {
    http_response_code($status);
    echo json_encode($body);
    exit;
}

avian_require_json_action();
avian_require_admin_proof();

$raw = file_get_contents('php://input');
if (!is_string($raw) || strlen($raw) > 4096) {
    respond(400, ['ok' => false, 'error' => 'invalid request']);
}
$body = json_decode($raw, true);
$file = is_array($body) ? trim((string)($body['file'] ?? '')) : '';
if (
    $file === '' || strlen($file) > 255 || strpos($file, '..') !== false ||
    basename($file) !== $file ||
    !preg_match("/^[^\\x00-\\x1f\\x7f\\/\\\\]+\\.mp3$/u", $file)
) {
    respond(400, ['ok' => false, 'error' => 'invalid recording filename']);
}

$command = 'sudo /usr/local/sbin/aviary-delete-recording --file ' . escapeshellarg($file) . ' 2>&1';
$lines = [];
$code = 5;
exec($command, $lines, $code);
$result = json_decode(implode("\n", $lines), true);
if (!is_array($result)) $result = ['ok' => false, 'error' => 'recording deletion failed'];

if ($code === 0 && !empty($result['ok'])) respond(200, $result);
if ($code === 2) respond(400, $result);
if ($code === 3) respond(404, $result);
if ($code === 4) respond(409, $result);
respond(500, ['ok' => false, 'error' => (string)($result['error'] ?? 'recording deletion failed')]);
