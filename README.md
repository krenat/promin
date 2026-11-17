<p align="center">
  <img src="static/logo.svg" width="96" height="96" alt="Promin logo">
</p>

<h1 align="center">Promin</h1>

<p align="center">A home video server for a projector, with your phone as the remote.</p>

Upload a video from your phone, the server transcodes it into a format the projector plays without buffering, and the phone becomes the remote control.

The interface is in Ukrainian.

**Status:** in development. Users with roles, upload, transcoding with ffmpeg, the video list, playback in the browser, deletion and the projector remote already work. The service runs over plain HTTP without encryption, so do not type passwords on networks you do not trust.

## Features

- upload videos from a phone through the browser;
- automatic transcoding with ffmpeg to 1080p H.264;
- a `/tv` page for the projector's browser and a `/` page for the phone;
- playback on the projector controlled from the phone over WebSocket;
- a shared watch queue: the projector plays the next video by itself;
- users with roles, and projectors paired with a code instead of a password.

## Stack

Python 3.12, FastAPI, Uvicorn, ffmpeg, Docker.

## Running locally

You need Python 3.9+ and ffmpeg with ffprobe in `PATH`. For correct colours in HDR videos, ffmpeg has to be built with libzimg (the `zscale` filter), as it is in Debian and Ubuntu.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PROMIN_TOKEN=local-dev-token-123 .venv/bin/python main.py
```

The page opens at `http://localhost:8080`; the login is `admin` and the password is the value of `PROMIN_TOKEN`. Tests: `.venv/bin/python -m pytest`.

## Running on a server with Docker

```bash
git clone https://github.com/krenat/promin.git
cd promin
cp .env.example .env   # set PROMIN_TOKEN and PROMIN_PORT
docker compose up -d --build
```

- The service listens on `PROMIN_PORT`; data lives in the `promin-data` volume.
- Logs: `docker compose logs -f`.
- Updating: `git pull && docker compose up -d --build`. With the old `docker-compose` 1.29 this command fails with `KeyError: 'ContainerConfig'`; use `docker-compose build && docker-compose down && docker-compose up -d` instead (without `-v`, so the volume with the videos is not deleted).
- The container does not run as root and is limited to `PROMIN_CPUS` cores.

## Configuration

All settings are environment variables; `.env.example` lists and describes them. The service does not start without `PROMIN_TOKEN` (at least 12 characters).

## Encoding settings

Any user chooses how new videos are encoded on the `/settings` page: the largest resolution (720p to 4K), the largest frame rate, the video bitrate ceiling, quality (CRF), the audio bitrate and the x264 preset.

- The settings apply to videos that start processing after they are saved. Videos that are already processed are not touched.
- A video is never enlarged: a smaller resolution, frame rate or bitrate than the limits stays as it is.
- A transcoded video does not get a higher bitrate than its original had. For HEVC, AV1 and VP9 originals the ceiling is 1.8 times the original bitrate, because H.264 needs more bits for the same picture.
- `PROMIN_MAX_BITRATE_KBPS` and `PROMIN_X264_PRESET` are the defaults until the settings are saved on the page.

## Projector and remote

1. Open `/tv` in the projector's browser. A six-digit code appears on the screen: on the phone, tap the pair-projector link and enter it. There is no password to type on the projector, and it is remembered as a trusted device.
2. Press the start button on the projector. This is needed once per session: without a press the browser does not allow playback with sound.
3. Open `/` on the phone. The indicator in the header turns yellow when the projector is connected.
4. Tap the cast button next to a video. A remote appears at the bottom: pause, seek by 10 seconds, a position slider, previous and next video, video volume, and a button that stops playback.

- A code is valid for 10 minutes and works once. A paired projector can only show videos: it cannot upload, delete or change settings; the list of paired projectors is on the `/users` page, and an administrator can disconnect any of them.
- There is one projector: if `/tv` is opened on a second screen, playback moves there.
- The projector page and the remote reconnect by themselves after a connection drop.
- The volume on the remote is the video's volume; the page cannot change the projector's system volume.
- Any logged-in user can control playback, viewers included.

### Watch queue

- The queue button on a video card adds it to the end of the queue. The same video can be queued more than once.
- The queue appears above the list: move entries up and down, remove them, clear the whole queue, or press play to start it on the projector.
- When a video ends (or cannot be played), the projector starts the next one from the queue by itself. With an empty queue it stops, as before.
- The next button on the remote takes the queue first and falls back to the next video in the library.
- There is one queue for the projector, shared by everyone. It survives a server restart, and deleted videos drop out of it.

## Users and roles

- The built-in administrator has the login `admin`; its password is the value of `PROMIN_TOKEN`.
- The administrator adds other users on the `/users` page. Their passwords are stored in the data volume only as hashes.
- A **viewer** watches, uploads and deletes videos, changes the encoding settings and controls the projector. An **administrator** can do all of that, also deletes several videos at once, and manages users and paired projectors.
- The video library is shared by everyone.
- Changing a password or deleting a user ends that user's sessions; changing `PROMIN_TOKEN` ends everyone's sessions.
- Logging out revokes the session on the server. A session that has not been used for 30 days ends by itself.
- After five failed login attempts from one address, login is blocked for 15 minutes; a successful login does not cancel earlier failures.

## How videos are processed

- A file that is already 8-bit SDR H.264 and fits the limits from the encoding settings (by default 1920×1080, 60 frames per second and a moderate bitrate) is only repackaged with `+faststart`.
- If the video already fits but the audio does not (not AAC, or more than two channels, such as AC3 5.1), the picture is copied unchanged and only the audio is re-encoded to stereo AAC. This is many times faster and loses no picture quality.
- Everything else is transcoded to H.264 High within those limits, with a bitrate cap, a keyframe every 2 seconds and AAC audio; HDR is converted to SDR.
- The original's metadata (including location) does not reach the finished file.
- The original is deleted after successful processing.

## License

MIT, see [LICENSE](LICENSE).
