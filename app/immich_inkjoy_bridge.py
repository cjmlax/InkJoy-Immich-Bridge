#!/usr/bin/env python3
"""
Immich -> InkJoy bridge (carousel mode), container edition.

All configuration comes from environment variables (see .env.example).

Runs in a loop: every SYNC_INTERVAL_MINUTES it re-pulls photos from Immich,
syncs them into a dedicated InkJoy album, and makes sure a carousel strategy
points the frame at that album. The frame advances photos on its own schedule
(the carousel's UPDATE_TIMES), independent of how often this bridge runs.

Set RUN_ONCE=true to run a single sync and exit (handy for testing or if you'd
rather schedule it with host cron instead of the internal loop).

The frame keeps doing its own ISFR rendering; this never touches color/dithering.
"""

import io
import os
import sys
import time
import traceback
import requests
from PIL import Image, ImageOps


# ===========================================================================
# Config from environment
# ===========================================================================

def env_str(key, default=""):
    return os.getenv(key, default).strip()

def env_int(key, default):
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def env_bool(key, default=False):
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

def env_list(key, default):
    v = os.getenv(key)
    if not v:
        return default
    return [x.strip() for x in v.split(",") if x.strip()]


# Immich
IMMICH_URL      = env_str("IMMICH_URL", "http://immich-host:2283/api")
IMMICH_API_KEY  = env_str("IMMICH_API_KEY")
IMMICH_ALBUM_ID = env_str("IMMICH_ALBUM_ID")
MAX_PHOTOS      = env_int("MAX_PHOTOS", 30)

# InkJoy
INKJOY_BASE       = env_str("INKJOY_BASE", "https://openapi.inkjoyframe.com")
INKJOY_EMAIL      = env_str("INKJOY_EMAIL")
INKJOY_PASSWORD   = env_str("INKJOY_PASSWORD")
INKJOY_DEVICE_ID  = env_str("INKJOY_DEVICE_ID") or None
INKJOY_ALBUM_NAME = env_str("INKJOY_ALBUM_NAME", "Immich")
TIMEZONE          = env_str("TIMEZONE", "America/New_York")

# Carousel behaviour
PLAY_ORDER    = env_str("PLAY_ORDER", "SHUFFLE")
UPDATE_TYPE   = env_str("UPDATE_TYPE", "FIXED")
UPDATE_TIMES  = env_list("UPDATE_TIMES", ["08:00", "13:00", "18:00", "22:00"])
INTERVAL_MIN  = env_int("INTERVAL_MIN", 120)
ACTIVE_BEGIN  = env_str("ACTIVE_BEGIN", "08:00")
ACTIVE_END    = env_str("ACTIVE_END", "22:00")
STRATEGY_TYPE = env_str("STRATEGY_TYPE", "TRIGGER_ON_DEVICE")
IDLE_AFTER    = env_int("IDLE_AFTER", 0)

# Image prep
RESIZE_TO_PANEL = env_bool("RESIZE_TO_PANEL", True)

# Scheduler
SYNC_INTERVAL_MINUTES = env_int("SYNC_INTERVAL_MINUTES", 720)   # how often the bridge refreshes the album
RUN_ONCE              = env_bool("RUN_ONCE", False)

REQ_TIMEOUT = 60


# ===========================================================================
# Immich client
# ===========================================================================

def immich_headers():
    return {"x-api-key": IMMICH_API_KEY, "Accept": "application/json"}


def pick_immich_assets():
    """Up to MAX_PHOTOS image asset IDs. Default: an Immich album.
    Alternatives: POST /search/smart, POST /search/metadata, GET /memories."""
    r = requests.get(f"{IMMICH_URL}/albums/{IMMICH_ALBUM_ID}",
                     headers=immich_headers(), timeout=REQ_TIMEOUT)
    r.raise_for_status()
    assets = [a for a in r.json().get("assets", []) if a.get("type") == "IMAGE"]
    return [a["id"] for a in assets[:MAX_PHOTOS]]


def immich_download(asset_id) -> bytes:
    r = requests.get(f"{IMMICH_URL}/assets/{asset_id}/original",
                     headers=immich_headers(), timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.content


def prepare_image(raw: bytes, panel_w, panel_h) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if RESIZE_TO_PANEL and panel_w and panel_h:
        longest = max(panel_w, panel_h)
        img.thumbnail((longest, longest), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


# ===========================================================================
# InkJoy client
# ===========================================================================

class InkJoy:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.token = None

    def _hdr(self):
        return {"Authorization": f"Bearer {self.token}", "timezone": TIMEZONE}

    def _unwrap(self, resp):
        resp.raise_for_status()
        j = resp.json()
        if isinstance(j, dict) and "code" in j and j["code"] not in (0, 200):
            raise RuntimeError(f"InkJoy API error {j['code']}: {j.get('msg')}")
        return j.get("data", j) if isinstance(j, dict) else j

    def login(self):
        r = requests.post(f"{self.base}/api/v1/auth/login",
                          json={"email": INKJOY_EMAIL, "password": INKJOY_PASSWORD},
                          timeout=REQ_TIMEOUT)
        self.token = self._unwrap(r)["token"]
        return self.token

    def list_devices(self):
        r = requests.get(f"{self.base}/api/v1/devices", headers=self._hdr(), timeout=REQ_TIMEOUT)
        return self._unwrap(r) or []

    def list_albums(self):
        r = requests.post(f"{self.base}/api/v1/album/list", headers=self._hdr(), timeout=REQ_TIMEOUT)
        return self._unwrap(r) or []

    def create_album(self, name):
        r = requests.post(f"{self.base}/api/v1/album", headers=self._hdr(),
                          json={"albumName": name}, timeout=REQ_TIMEOUT)
        return self._unwrap(r)["albumId"]

    def list_album_photos(self, album_id):
        r = requests.post(f"{self.base}/api/v1/album/img/list", headers=self._hdr(),
                          json={"albumId": album_id}, timeout=REQ_TIMEOUT)
        return self._unwrap(r) or []

    def add_photo(self, album_id, jpeg_bytes, filename="photo.jpg"):
        files = {"file": (filename, jpeg_bytes, "image/jpeg")}
        data = {"albumId": album_id}
        r = requests.post(f"{self.base}/api/v1/album/img", headers=self._hdr(),
                          files=files, data=data, timeout=REQ_TIMEOUT)
        return self._unwrap(r)

    def remove_photos(self, album_id, img_ids):
        if not img_ids:
            return
        r = requests.post(f"{self.base}/api/v1/album/img/del", headers=self._hdr(),
                          json={"albumId": album_id, "imgIdList": img_ids}, timeout=REQ_TIMEOUT)
        return self._unwrap(r)

    def list_strategies(self, device_id):
        r = requests.post(f"{self.base}/api/v1/devicePlayStrategy/list", headers=self._hdr(),
                          json={"deviceId": device_id}, timeout=REQ_TIMEOUT)
        return self._unwrap(r) or []

    def create_strategy(self, body):
        r = requests.post(f"{self.base}/api/v1/devicePlayStrategy", headers=self._hdr(),
                          json=body, timeout=REQ_TIMEOUT)
        return self._unwrap(r)

    def update_strategy(self, strategy_id, body):
        r = requests.put(f"{self.base}/api/v1/devicePlayStrategy/{strategy_id}",
                         headers=self._hdr(), json=body, timeout=REQ_TIMEOUT)
        return self._unwrap(r)


# ===========================================================================
# Orchestration
# ===========================================================================

def find_device(inkjoy):
    devices = inkjoy.list_devices()
    if not devices:
        raise RuntimeError("No InkJoy devices bound to this account. Bind the frame in the app first.")
    if INKJOY_DEVICE_ID:
        dev = next((d for d in devices if d["deviceId"] == INKJOY_DEVICE_ID), None)
        if not dev:
            raise RuntimeError(f"Device {INKJOY_DEVICE_ID} not found. Available: "
                               + ", ".join(d['deviceId'] for d in devices))
        return dev
    return devices[0]


def ensure_album(inkjoy, name):
    for a in inkjoy.list_albums():
        if a.get("albumName") == name:
            return a["albumId"]
    return inkjoy.create_album(name)


def build_strategy_body(device_id, album_id):
    body = {
        "deviceId": device_id,
        "albumIdList": [album_id],
        "playOrder": PLAY_ORDER,
        "timezone": TIMEZONE,
        "strategyType": STRATEGY_TYPE,
        "updateType": UPDATE_TYPE,
        "updateDays": UPDATE_DAYS,
        "idle": IDLE_AFTER,
        "playNow": True,
        "status": "ACTIVE",
        
    }
    if UPDATE_TYPE == "FIXED":
        body["updateTimeList"] = UPDATE_TIMES
    else:
        body["beginTime"] = ACTIVE_BEGIN
        body["endTime"] = ACTIVE_END
        body["intervalMinutes"] = INTERVAL_MIN
    return body


def sync_once():
    inkjoy = InkJoy(INKJOY_BASE)
    inkjoy.login()
    print("Authenticated with InkJoy.", flush=True)

    dev = find_device(inkjoy)
    res = dev.get("resolution") or {}
    pw, ph = res.get("width"), res.get("height")
    battery = (dev.get("currentStatus") or {}).get("battery", "?")
    print(f"Device: {dev.get('deviceName')} ({dev['deviceId']})  {pw}x{ph}  battery={battery}%", flush=True)

    album_id = ensure_album(inkjoy, INKJOY_ALBUM_NAME)
    print(f"Album '{INKJOY_ALBUM_NAME}' -> {album_id}", flush=True)

    existing = inkjoy.list_album_photos(album_id)
    if existing:
        inkjoy.remove_photos(album_id, [p["imgId"] for p in existing])
        print(f"Cleared {len(existing)} old photo(s).", flush=True)

    asset_ids = pick_immich_assets()
    print(f"Uploading {len(asset_ids)} photo(s) from Immich...", flush=True)
    for i, aid in enumerate(asset_ids, 1):
        jpeg = prepare_image(immich_download(aid), pw, ph)
        inkjoy.add_photo(album_id, jpeg, filename=f"{aid}.jpg")
        print(f"  [{i}/{len(asset_ids)}] {aid}", flush=True)

    body = build_strategy_body(dev["deviceId"], album_id)
    strategies = inkjoy.list_strategies(dev["deviceId"])
    mine = next((s for s in strategies if album_id in (s.get("albumIdList") or [])), None)
    if mine:
        inkjoy.update_strategy(mine["strategyId"], body)
        print(f"Updated carousel {mine['strategyId']}.", flush=True)
    else:
        inkjoy.create_strategy(body)
        print("Created carousel.", flush=True)

    print("Sync complete.", flush=True)


def main():
    # Fail fast on missing secrets so the container logs a clear message.
    missing = [k for k in ("IMMICH_API_KEY", "INKJOY_EMAIL", "INKJOY_PASSWORD")
               if not env_str(k)]
    if missing:
        sys.exit("Missing required env vars: " + ", ".join(missing))

    while True:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== Sync run @ {started} ===", flush=True)
        try:
            sync_once()
        except Exception as e:
            print(f"Sync failed: {e}", flush=True)
            traceback.print_exc()
        if RUN_ONCE:
            break
        print(f"Sleeping {SYNC_INTERVAL_MINUTES} min until next sync.", flush=True)
        time.sleep(SYNC_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
