# Immich -> InkJoy bridge

Mirrors photos from a self-hosted Immich album into a dedicated InkJoy album and
keeps a carousel "play strategy" pointed at it, so an InkJoy ePaper frame cycles
your Immich photos. The frame's own ISFR rendering is left untouched.

Runs as a long-lived container that re-syncs on a schedule. The InkJoy app is
only needed once, to bind the frame to your account.

## Configure
Copy `.env.example` to `.env` and fill it in. `.env` is gitignored — keep your
keys out of the repo.

## Run (Dockge / Docker Compose)

**Method A — local build (files in the stack folder)**
Put `compose.yaml`, `Dockerfile`, the `app/` folder, and `.env` in one Dockge
stack directory and Deploy.

**Method B — build from GitHub**
Push this repo to GitHub, then in `compose.yaml` swap the `build: .` line for:
```
build: https://github.com/YOUR_USER/immich-inkjoy-bridge.git#main
```
Now the stack only needs `compose.yaml` + `.env`; recreate the stack to pull
updates after you push. (Public repos work directly; for a private repo, build
the image in CI and push to a registry, then use `image:` instead of `build:`.)

## Test before scheduling
Set `RUN_ONCE=true` and `MAX_PHOTOS=3`, deploy, and watch the logs for one full
sync. Then set them back.

## Notes
- `SYNC_INTERVAL_MINUTES` = how often the bridge refreshes the album from Immich.
- `UPDATE_TIMES` / `INTERVAL_MIN` = how often the frame advances photos (InkJoy side).
- If the InkJoy calls return an error code, the log prints the API `msg`.
