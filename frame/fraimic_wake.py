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


def reachable(base_url, timeout):
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/battery",
        headers={"User-Agent": "Aviary-Fraimic-Wake/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(10_000)
            return 200 <= response.status < 300
    except OSError:
        return False


def watch(cfg, interval, probe_timeout, upload_timeout, refresh_seconds):
    payload = prepare_payload(cfg)
    prepared_at = datetime.now().astimezone()
    next_refresh = time.monotonic() + refresh_seconds
    awake = False
    uploaded = False
    print(f"{prepared_at.isoformat()} ARMED interval={interval}s "
          f"payload_bytes={len(payload)}", flush=True)

    while True:
        is_reachable = reachable(cfg["fraimic_url"], probe_timeout)
        now = datetime.now().astimezone()
        if is_reachable:
            if not awake:
                print(f"{now.isoformat()} WAKE_DETECTED "
                      f"payload_prepared={prepared_at.isoformat()}", flush=True)
            awake = True
            if not uploaded:
                try:
                    body = send(payload, cfg["fraimic_url"], upload_timeout)
                    try:
                        status = json.loads(body).get("status", "accepted")
                    except json.JSONDecodeError:
                        status = "accepted"
                    print(f"{datetime.now().astimezone().isoformat()} "
                          f"UPLOAD_OK status={status} bytes={len(payload)}",
                          flush=True)
                    uploaded = True
                except OSError as exc:
                    print(f"{datetime.now().astimezone().isoformat()} "
                          f"UPLOAD_RETRY error={exc}", flush=True)
        else:
            if awake:
                print(f"{now.isoformat()} SLEEP_DETECTED uploaded={uploaded}",
                      flush=True)
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
