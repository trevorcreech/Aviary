#!/usr/bin/env python3
"""Render the Aviary collage and send it to a Fraimic over its local API.

This is deliberately a thin adapter around the existing frame pipeline: it
reuses display.py's species/change detection and shoot.py's live-site render.
Fraimic's cloud account remains paired; this script only uses the LAN API to
replace the image currently shown on the panel.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

from PIL import Image, ImageChops

from display import (_auth, fetch_species, in_quiet_hours, load_state,
                     save_state, signature)

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


RAW_SIZE = (1200, 1600)
PALETTE = [
    (0, 0, 0),        # black  -> 0x0
    (255, 255, 255),  # white  -> 0x1
    (255, 255, 0),    # yellow -> 0x2
    (255, 0, 0),      # red    -> 0x3
    (0, 0, 255),      # blue   -> 0x5
    (0, 255, 0),      # green  -> 0x6
]
COLOR_CODES = (0x0, 0x1, 0x2, 0x3, 0x5, 0x6)
ORIENTATIONS = {
    "portrait": None,
    # The frame was turned left when viewed from the front, so its original
    # bottom edge is now on the right. Rotate the logical image clockwise into
    # Fraimic's required portrait framebuffer.
    "landscape": Image.Transpose.ROTATE_270,
}

DEFAULTS = {
    "base_url": "http://aviary.local",
    "fraimic_url": "http://fraimic.local",
    "orientation": "portrait",
    "species_source": "",
    "zip": "",
    "bw_station_id": "",
    "bw_days": 7,
    "bw_country": "us",
    "hours": 24,
    "shoot_title": "Aviary",
    "shoot_subtitle": "Heard Today",
    "shoot_headline_px": 42,
    "shoot_eyebrow_px": 18,
    "shoot_lowercase": False,
    "shoot_mat": 0.04,
    "shoot_small_floor": 0.04,
    "shoot_count_exp": 0.65,
    "shoot_collage_vh": 52,
    "shoot_collage_scale": 1.0,
    "bird_names": False,
    "quiet_start": 0,
    "quiet_end": 0,
    "heal_hours": 24,
    "state": "~/.birdframe/fraimic-state.json",
    "cache": "~/.birdframe",
    "timeout": 180,
    "basic_user": None,
    "basic_pass": None,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    with open(os.path.expanduser(path), "rb") as f:
        cfg.update(tomllib.load(f))
    if cfg["orientation"] not in ORIENTATIONS:
        choices = ", ".join(ORIENTATIONS)
        raise ValueError(f"orientation must be one of: {choices}")
    return cfg


def logical_size(orientation):
    return (1600, 1200) if orientation == "landscape" else RAW_SIZE


def render(cfg, species=None):
    """Render the existing website at the physical frame's aspect ratio."""
    from shoot import shoot, shoot_birdweather

    width, height = logical_size(cfg["orientation"])
    out = os.path.join(os.path.expanduser(cfg["cache"]), "fraimic.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    look = {
        "vw": width // 2,
        "vh": height // 2,
        "dsf": 2,
        "headline_px": cfg["shoot_headline_px"],
        "eyebrow_px": cfg["shoot_eyebrow_px"],
        "lowercase": cfg["shoot_lowercase"],
        "mat": cfg["shoot_mat"],
        "collage_vh": cfg["shoot_collage_vh"],
        "collage_scale": cfg["shoot_collage_scale"],
        "small_floor": cfg["shoot_small_floor"],
        "count_exp": cfg["shoot_count_exp"],
        "window_hours": cfg["hours"],
        "bird_names": cfg["bird_names"],
        "timeout_ms": cfg["timeout"] * 1000,
    }
    if cfg.get("species_source") == "birdweather":
        if species is None:
            species = fetch_species(cfg, _auth(cfg))
        shoot_birdweather(out, species, title=cfg["shoot_title"],
                          subtitle=cfg["shoot_subtitle"], **look)
    else:
        shoot(cfg["base_url"], out, title=cfg["shoot_title"],
              subtitle=cfg["shoot_subtitle"], user=cfg["basic_user"],
              password=cfg["basic_pass"], **look)
    img = Image.open(out).convert("RGB")
    if img.size != (width, height):
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    return img


def orient_for_api(img, orientation):
    """Turn a physically upright image into Fraimic's 1200x1600 raw axes."""
    expected = logical_size(orientation)
    if img.size != expected:
        raise ValueError(f"{orientation} image must be {expected[0]}x{expected[1]}, got {img.size}")
    transpose = ORIENTATIONS[orientation]
    raw = img if transpose is None else img.transpose(transpose)
    if raw.size != RAW_SIZE:
        raise AssertionError(f"oriented image is {raw.size}, expected {RAW_SIZE}")
    return raw


def clean_palette_extremes(img):
    """Make nearly white backgrounds and very dark ink solid before dithering."""
    rgb = img.convert("RGB")
    red, green, blue = rgb.split()
    white_lut = [0] * 245 + [255] * 11
    black_lut = [255] * 81 + [0] * 175
    near_white = ImageChops.darker(
        ImageChops.darker(red.point(white_lut), green.point(white_lut)),
        blue.point(white_lut))
    near_black = ImageChops.darker(
        ImageChops.darker(red.point(black_lut), green.point(black_lut)),
        blue.point(black_lut))
    cleaned = rgb.copy()
    cleaned.paste((255, 255, 255), mask=near_white)
    cleaned.paste((0, 0, 0), mask=near_black)
    return cleaned


def quantize(img):
    palette_image = Image.new("P", (1, 1))
    flat = [channel for color in PALETTE for channel in color]
    palette_image.putpalette(flat + [0, 0, 0] * (256 - len(PALETTE)))
    return clean_palette_extremes(img).quantize(
        palette=palette_image, dither=Image.Dither.FLOYDSTEINBERG)


def pack(indices):
    """Pack a 1200x1600 indexed image in Fraimic's split-half layout."""
    if indices.size != RAW_SIZE:
        raise ValueError(f"Fraimic image must be {RAW_SIZE[0]}x{RAW_SIZE[1]}")
    if indices.mode != "P":
        raise ValueError("Fraimic packer needs an indexed (P mode) image")

    table = bytes(COLOR_CODES) + bytes(256 - len(COLOR_CODES))
    pixels = indices.tobytes().translate(table)
    packed = bytearray()
    packed_extend = packed.extend
    for x0 in (0, 600):
        for y in range(1600):
            start = y * 1200 + x0
            row = pixels[start:start + 600]
            packed_extend((row[x] << 4) | row[x + 1] for x in range(0, 600, 2))
    if len(packed) != 960_000:
        raise AssertionError(f"packed image is {len(packed)} bytes, expected 960000")
    return bytes(packed)


def send(payload, base_url, timeout):
    url = base_url.rstrip("/") + "/api/image"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "User-Agent": "Aviary-Fraimic/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read(100_000).decode("utf-8", "replace")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Fraimic returned HTTP {response.status}: {body}")
    return body


def run(cfg, *, preview=None, force=False, use_signature=True):
    now = time.time()
    state = load_state(cfg["state"])
    sig = None
    species = None
    if use_signature:
        try:
            species = fetch_species(cfg, _auth(cfg))
            sig = signature(species)
        except Exception as exc:
            print(f"signature fetch failed: {exc}", file=sys.stderr)
    heal_due = now - state.get("last_refresh", 0) >= cfg["heal_hours"] * 3600
    changed = (not use_signature) or (sig is not None and sig != state.get("signature"))
    if not force and not preview:
        if in_quiet_hours(cfg, datetime.now().hour):
            print("quiet hours; skip")
            return
        if not changed and not heal_due:
            print("no change; skip")
            return
        print("refresh:", "changed" if changed else "heal")

    logical = render(cfg, species)
    raw = orient_for_api(logical, cfg["orientation"])
    indices = quantize(raw)
    if preview:
        indices.convert("RGB").save(preview)
        print(f"wrote Fraimic-oriented preview {preview}")
        return

    response = send(pack(indices), cfg["fraimic_url"], cfg["timeout"])
    try:
        status = json.loads(response).get("status", "accepted")
    except json.JSONDecodeError:
        status = "accepted"
    save_state(cfg["state"], sig if sig is not None else state.get("signature"), now)
    print(f"Fraimic update {status}")


def main():
    parser = argparse.ArgumentParser(description="Send the Aviary collage to a Fraimic.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--preview", help="write the oriented six-color PNG instead of sending")
    parser.add_argument("--force", action="store_true", help="refresh even if birds are unchanged")
    parser.add_argument("--no-signature", action="store_true", help="skip change detection")
    args = parser.parse_args()

    cfg = load_config(args.config)
    lock_path = os.path.join(os.path.expanduser(cfg["cache"]), ".fraimic-render.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another Fraimic render is in progress; skipping")
        return
    run(cfg, preview=args.preview, force=args.force,
        use_signature=not args.no_signature)


if __name__ == "__main__":
    main()
