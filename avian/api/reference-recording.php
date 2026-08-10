<?php
// Aviary - representative species recording from Macaulay Library/eBird.
//
// The detail card asks for a scientific name. We resolve that name through
// Cornell's public taxonomy service, then choose the highest-rated public
// audio result that was not tagged as playback. Only metadata is returned;
// the browser streams the MP3 directly from Cornell's media CDN.

declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: public, max-age=604800');

function respond(array $body, int $status = 200): never
{
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_SLASHES);
    exit;
}

function fetch_json(string $url): ?array
{
    $ua = getenv('AV_USER_AGENT') ?: 'Aviary/1.0 (+https://github.com/trevorcreech/Aviary)';
    $ctx = stream_context_create([
        'http' => [
            'header' => "User-Agent: $ua\r\nAccept: application/json\r\n",
            'timeout' => 8,
            'ignore_errors' => true,
        ],
    ]);
    $raw = @file_get_contents($url, false, $ctx);
    if ($raw === false) {
        return null;
    }
    $decoded = json_decode($raw, true);
    return is_array($decoded) ? $decoded : null;
}

$sci = trim((string)($_GET['sci'] ?? ''));
if (!preg_match('/^[A-Z][A-Za-z-]{1,39}(?:[ ][a-z][A-Za-z-]{1,39}){1,3}$/', $sci)) {
    respond(['error' => 'invalid sci'], 400);
}

// Cache the tiny resolved response on the Pi. Audio remains hotlinked.
$cacheFile = sys_get_temp_dir() . '/aviary-reference-' . hash('sha256', $sci) . '.json';
if (is_file($cacheFile) && filemtime($cacheFile) > time() - 604800) {
    $cached = @file_get_contents($cacheFile);
    $cachedJson = $cached === false ? null : json_decode($cached, true);
    if (is_array($cachedJson)) {
        respond($cachedJson);
    }
}

// This is Cornell's public browser key, exposed by the Macaulay Library
// search application for taxonomy autocomplete (not a user secret).
$taxonomyUrl = 'https://taxonomy.api.macaulaylibrary.org/ws5.0/taxonomy-all?' . http_build_query([
    'key' => 'PUB5447877383',
    'taxaLocale' => 'en',
    'q' => $sci,
]);
$taxa = fetch_json($taxonomyUrl);
if ($taxa === null) {
    respond(['error' => 'reference service unavailable'], 502);
}

$taxonCode = null;
foreach ($taxa as $taxon) {
    if (!is_array($taxon)) {
        continue;
    }
    $name = (string)($taxon['name'] ?? '');
    $code = explode(',', (string)($taxon['code'] ?? ''), 2)[0];
    // Suggestion labels end in " - <scientific name>". Require the exact
    // suffix so a hybrid or similarly named taxon cannot be selected.
    if ($code !== '' && str_ends_with($name, ' - ' . $sci)) {
        $taxonCode = $code;
        break;
    }
}

if ($taxonCode === null) {
    $body = ['available' => false];
    @file_put_contents($cacheFile, json_encode($body, JSON_UNESCAPED_SLASHES), LOCK_EX);
    respond($body, 404);
}

$searchUrl = 'https://search.macaulaylibrary.org/api/v2/search?' . http_build_query([
    'taxonCode' => $taxonCode,
    'mediaType' => 'audio',
    'sort' => 'rating_rank_desc',
    'count' => 25,
]);
$results = fetch_json($searchUrl);
if ($results === null) {
    respond(['error' => 'reference service unavailable'], 502);
}

$pick = null;
foreach ($results as $result) {
    if (!is_array($result) || ($result['valid'] ?? false) !== true || ($result['restricted'] ?? false) === true) {
        continue;
    }
    $tags = is_array($result['tags'] ?? null) ? $result['tags'] : [];
    if (in_array('playback', $tags, true)) {
        continue;
    }
    $assetId = (int)($result['assetId'] ?? 0);
    if ($assetId > 0) {
        $pick = $result;
        break;
    }
}

if ($pick === null) {
    $body = ['available' => false];
    @file_put_contents($cacheFile, json_encode($body, JSON_UNESCAPED_SLASHES), LOCK_EX);
    respond($body, 404);
}

$assetId = (int)$pick['assetId'];
$tags = is_array($pick['tags'] ?? null) ? $pick['tags'] : [];
$soundType = 'reference';
foreach (['song', 'call', 'flight_call', 'dawn_song', 'flight_song'] as $candidate) {
    if (in_array($candidate, $tags, true)) {
        $soundType = str_replace('_', ' ', $candidate);
        break;
    }
}

$body = [
    'available' => true,
    'asset_id' => $assetId,
    'audio_url' => 'https://cdn.download.ams.birds.cornell.edu/api/v2/asset/' . $assetId . '/mp3',
    'asset_url' => 'https://macaulaylibrary.org/asset/' . $assetId,
    'source' => 'Macaulay Library',
    'contributor' => trim((string)($pick['userDisplayName'] ?? '')),
    'sound_type' => $soundType,
];
@file_put_contents($cacheFile, json_encode($body, JSON_UNESCAPED_SLASHES), LOCK_EX);
respond($body);
