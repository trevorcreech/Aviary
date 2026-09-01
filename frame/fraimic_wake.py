#!/usr/bin/env python3
"""Pre-render a timestamp card and send it when a sleeping Fraimic wakes."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from fraimic import (load_config, logical_size, orient_for_api, pack, quantize,
                     send)


def _font(size, bold=False):
    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for name in names:
        if os.path.exists(name):
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def timestamp_card(orientation, now=None):
    """Return a logical-orientation wake-test image for approximately *now*."""
    now = now or datetime.now().astimezone()
    width, height = logical_size(orientation)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    margin = int(min(width, height) * 0.07)
    draw.rounded_rectangle(
        (margin, margin, width - margin, height - margin),
        radius=28, outline="black", width=6)
    draw.text((width / 2, height * 0.20), "AVIARY  /  WAKE TEST",
              fill="black", font=_font(int(height * 0.035), bold=True),
              anchor="mm")
    draw.text((width / 2, height * 0.47), now.strftime("%-I:%M %p"),
              fill="black", font=_font(int(height * 0.14), bold=True),
              anchor="mm")
    draw.text((width / 2, height * 0.61), now.strftime("%A, %B %-d"),
              fill="black", font=_font(int(height * 0.045)), anchor="mm")
    draw.text((width / 2, height * 0.80), "Prepared within the last minute",
              fill="black", font=_font(int(height * 0.028)), anchor="mm")
    return image


def prepare_payload(cfg, now=None):
    logical = timestamp_card(cfg["orientation"], now)
    raw = orient_for_api(logical, cfg["orientation"])
    return pack(quantize(raw))


def api_json(base_url, endpoint, timeout):
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/" + endpoint,
        headers={"User-Agent": "Aviary-Fraimic-Wake/1.0"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(100_000).decode("utf-8", "replace")
            if response.status < 200 or response.status >= 300:
                return None, round((time.monotonic() - started) * 1000), \
                    f"HTTP {response.status}"
            return json.loads(body), \
                round((time.monotonic() - started) * 1000), None
    except (OSError, ValueError) as exc:
        return None, round((time.monotonic() - started) * 1000), str(exc)


def _find_int(value, key):
    if isinstance(value, dict):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                pass
        for child in value.values():
            found = _find_int(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_int(child, key)
            if found is not None:
                return found
    return None


def render_counts(base_url, timeout):
    info, elapsed_ms, error = api_json(base_url, "info", timeout)
    if info is None:
        return None, elapsed_ms, error
    attempts = _find_int(info, "render_attempts")
    failures = _find_int(info, "render_failures")
    return (attempts, failures, info), elapsed_ms, None


def watch(cfg, interval, probe_timeout, upload_timeout, refresh_seconds):
    payload = prepare_payload(cfg)
    prepared_at = datetime.now().astimezone()
    next_refresh = time.monotonic() + refresh_seconds
    awake = False
    uploaded = False
    last_counts = None
    wake_started = None
    upload_tries = 0
    print(f"{prepared_at.isoformat()} ARMED interval={interval}s "
          f"payload_bytes={len(payload)}", flush=True)

    while True:
        battery, probe_ms, probe_error = api_json(
            cfg["fraimic_url"], "battery", probe_timeout)
        is_reachable = battery is not None
        now = datetime.now().astimezone()
        if is_reachable:
            if not awake:
                wake_started = time.monotonic()
                upload_tries = 0
                print(f"{now.isoformat()} WAKE_DETECTED "
                      f"probe_ms={probe_ms} "
                      f"payload_prepared={prepared_at.isoformat()} "
                      f"payload_age_s={(now - prepared_at).total_seconds():.1f} "
                      f"battery={json.dumps(battery, separators=(',', ':'))}",
                      flush=True)
                last_counts = None
            awake = True
            if not uploaded:
                upload_tries += 1
                upload_started = time.monotonic()
                print(f"{datetime.now().astimezone().isoformat()} "
                      f"UPLOAD_START try={upload_tries} bytes={len(payload)}",
                      flush=True)
                try:
                    body = send(payload, cfg["fraimic_url"], upload_timeout)
                    try:
                        status = json.loads(body).get("status", "accepted")
                    except json.JSONDecodeError:
                        status = "accepted"
                    print(f"{datetime.now().astimezone().isoformat()} "
                          f"UPLOAD_OK try={upload_tries} status={status} "
                          f"bytes={len(payload)} "
                          f"elapsed_ms={round((time.monotonic() - upload_started) * 1000)} "
                          f"response={body!r}",
                          flush=True)
                    uploaded = True
                except OSError as exc:
                    print(f"{datetime.now().astimezone().isoformat()} "
                          f"UPLOAD_RETRY try={upload_tries} "
                          f"elapsed_ms={round((time.monotonic() - upload_started) * 1000)} "
                          f"error={exc!r}", flush=True)
            if uploaded:
                result, info_ms, info_error = render_counts(
                    cfg["fraimic_url"], probe_timeout)
                if result is not None:
                    attempts, failures, info = result
                    counts = attempts, failures
                    print(f"{datetime.now().astimezone().isoformat()} "
                          f"INFO_SNAPSHOT query_ms={info_ms} "
                          f"attempts={attempts} failures={failures} "
                          f"changed={counts != last_counts} "
                          f"info={json.dumps(info, separators=(',', ':'))}",
                          flush=True)
                    last_counts = counts
                else:
                    print(f"{datetime.now().astimezone().isoformat()} "
                          f"INFO_UNAVAILABLE query_ms={info_ms} "
                          f"error={info_error!r}", flush=True)
        else:
            if awake:
                awake_s = time.monotonic() - wake_started
                print(f"{now.isoformat()} SLEEP_DETECTED uploaded={uploaded} "
                      f"upload_tries={upload_tries} awake_s={awake_s:.1f} "
                      f"last_counts={last_counts} probe_ms={probe_ms} "
                      f"probe_error={probe_error!r}", flush=True)
            awake = False
            uploaded = False
            if time.monotonic() >= next_refresh:
                payload = prepare_payload(cfg, now)
                prepared_at = now
                next_refresh = time.monotonic() + refresh_seconds
                print(f"{now.isoformat()} PAYLOAD_REFRESHED", flush=True)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="Upload a prepared timestamp card when a Fraimic wakes.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--probe-timeout", type=float, default=0.7)
    parser.add_argument("--upload-timeout", type=float, default=3.0)
    parser.add_argument("--refresh-seconds", type=float, default=60.0)
    parser.add_argument("--prepare-only",
                        help="write one packed payload and exit")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.prepare_only:
        payload = prepare_payload(cfg)
        with open(os.path.expanduser(args.prepare_only), "wb") as output:
            output.write(payload)
        print(f"wrote {len(payload)} bytes to {args.prepare_only}")
        return
    watch(cfg, args.interval, args.probe_timeout, args.upload_timeout,
          args.refresh_seconds)


if __name__ == "__main__":
    main()
