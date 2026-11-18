<p align="center">
  <img src="promin/static/logo.svg" width="96" height="96" alt="Promin logo">
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
- folders, one level deep;
- a shared watch queue: the projector plays the next video by itself;
- users with roles, and projectors paired with a QR code instead of a password.

## Stack

Python 3.12, FastAPI, Uvicorn, ffmpeg, Docker.

## Code layout

`main.py` only starts the server; the code is in the `promin` package.

| Module | What is in it |
|---|---|
| `config` | settings from the environment, paths |
| `db` | the SQLite database: transactions, the copy kept at every start, recovery of a damaged file |
| `legacy` | bringing over the JSON files that older versions kept their data in |
| `library` | the video list, folders and the watch queue |
| `encoding` | the encoding settings |
| `ffmpeg`, `transcode` | the plan and the ffmpeg commands; the worker that runs them |
| `upload` | what may be uploaded, and writing it to disk |
| `accounts`, `devices` | users and their sessions; paired projectors |
| `auth`, `crypto` | who is asking, by the cookie; the login limit; hashing |
| `hub` | the live connections of the projector page and the phones |
| `playback` | the watch queue as phones see it, and what the projector plays next |
| `tvtest` | the projector check |
| `services` | all of the above for one running app, and what spans several of them |
| `assets` | serving `static/` under a version, so browsers do not mix old and new files |
| `routes/` | the pages and the API, one module per subject |
| `templates/` | the pages: markup only |
| `static/` | the logo, and the styles (`css/`) and scripts (`js/`) of each page |
| `app` | `create_app`, which puts it together |

Every route names who may call it (`AUTH`, `MEMBER` or `ADMIN` from `routes/deps.py`); a test fails if one is left open by accident.

Pages name their styles and scripts under a version (`/static/v-<version>/...`) that changes whenever a file in `static/` does, so a browser never runs an old script against a new page.

The scripts are plain JavaScript without a build step. The phone page's script is split into ES modules in `static/js/index/` (`main.js` starts it; the shared state is in `state.js`). `api.js` is the one way every page calls the server. With Node.js installed, the tests also load every module script in it; without it those tests are skipped. `tv.js` stays one file without newer language features, because the projector's browser may be old.

The tests follow the same subjects: `tests/test_<subject>.py`, with the shared fixtures in `conftest.py` and the shared steps in `helpers.py`.

## Running locally

You need Python 3.12 (the version in the Docker image; `requirements.txt` pins exact package versions) and ffmpeg with ffprobe in `PATH`. For correct colours in HDR videos, ffmpeg has to be built with libzimg (the `zscale` filter), as it is in Debian and Ubuntu.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PROMIN_TOKEN=local-dev-token-123 .venv/bin/python main.py
```

The page opens at `http://localhost:8080`; the login is `admin` and the password is the value of `PROMIN_TOKEN`. Tests: `.venv/bin/python -m pytest`. GitHub Actions runs them on every pull request with the pinned versions.

## Running on a server with Docker

```bash
git clone https://github.com/krenat/promin.git
cd promin
cp .env.example .env   # set PROMIN_TOKEN and PROMIN_PORT
docker compose up -d --build
```

- The service listens on `PROMIN_PORT`; data lives in the `promin-data` volume: the video files, and `promin.db` (SQLite) with everything else. Every start keeps a copy of the database as `promin.db.bak`; a database that cannot be read is moved aside as `promin.db.broken-<time>` and the copy is used.
- A version that kept its data in JSON files (`videos.json`, `users.json` and the others) is brought over by itself on the first start: the files are read into the database once and renamed to `.imported`.
- Logs: `docker compose logs -f`.
- Updating: `git pull && docker compose up -d --build`. With the old `docker-compose` 1.29 this command fails with `KeyError: 'ContainerConfig'`; use `docker-compose build && docker-compose down && docker-compose up -d` instead (without `-v`, so the volume with the videos is not deleted).
- The container does not run as root and is limited to `PROMIN_CPUS` cores.
- Behind a reverse proxy that adds HTTPS, set `FORWARDED_ALLOW_IPS` (see `.env.example`): without it every visitor looks like the proxy, and the login limit blocks everyone at once.

## Configuration

All settings are environment variables; `.env.example` lists and describes them. The service does not start without `PROMIN_TOKEN` (at least 12 characters).

## Folders

- Folders are one level deep, and a video is in at most one folder.
- The row of folders above the list filters it: all videos, one folder, or videos without a folder. The phone remembers the last choice.
- Create a folder with the plus at the end of the row; rename or delete the folder that is open with the buttons under the row. Deleting a folder keeps its videos: they stay in the library without a folder.
- Move a video with "move to folder" in its card menu. A video uploaded while a folder is open goes into that folder.
- Any user with a login manages folders; a paired projector cannot.

## Encoding settings

Any user chooses how new videos are encoded on the `/settings` page: the largest resolution (720p to 4K), the largest frame rate, the video bitrate ceiling, quality (CRF), the audio bitrate and the x264 preset.

- The settings apply to videos that start processing after they are saved. Videos that are already processed are not touched.
- A video is never enlarged: a smaller resolution, frame rate or bitrate than the limits stays as it is.
- A transcoded video does not get a higher bitrate than its original had. For HEVC, AV1 and VP9 originals the ceiling is 1.8 times the original bitrate, because H.264 needs more bits for the same picture.
- `PROMIN_MAX_BITRATE_KBPS` and `PROMIN_X264_PRESET` are the defaults until the settings are saved on the page.

### Projector check

The same page has a "check the projector" button that shows which of these settings the projector can actually play. The projector must be on the `/tv` page, started, and not playing a video.

- The server generates an 8-second H.264 clip for every resolution and frame rate (once, into `tvtest/` in the data directory), encoded at the bitrate the page suggests for that resolution.
- The projector measures its download speed, then downloads each clip whole and plays it muted for 6 seconds. A clip counts as smooth when playback kept up with the clock and at most 5% of the frames were dropped. A harder clip is tried only after the easier one played smoothly.
- The page shows the verdict for every clip, the measured speed, and the browser the projector reported, and warns when the chosen settings go past what the check found. Half of the measured speed is treated as the safe bitrate ceiling.
- Some TV browsers do not count dropped frames; then only the clock is judged and the page says so. The check says what the browser can decode, not the native resolution of the picture.

## Projector and remote

1. Open `/tv` in the projector's browser. A QR code appears on the screen: point the phone's camera at it, open the link and confirm on the page that opens (after logging in, if the phone is not logged in yet). There is nothing to type on the projector, and it is remembered as a trusted device.
2. Press the start button on the projector. This is needed once per session: without a press the browser does not allow playback with sound.
3. Open `/` on the phone. The indicator in the header turns yellow when the projector is connected.
4. Tap the cast button next to a video. A remote appears at the bottom: pause, seek by 10 seconds, a position slider, previous and next video, video volume, and a button that stops playback.

- A QR code is valid for 10 minutes and works once. Confirm only a code you scanned from your own projector: a pairing link sent by someone else would let their screen show your videos. A paired projector can only show videos: it cannot upload, delete, change settings or act as a remote; the list of paired projectors is on the `/users` page, and an administrator can disconnect any of them.
- There is one projector: if `/tv` is opened on a second screen, playback moves there.
- Any screen with `/tv` open is the projector, a laptop included. To send the wrong one away, tap the projector indicator in the header of the phone page and press the disconnect button: that screen stops showing until someone there chooses to show again. A paired projector stays paired; unpairing is done on the `/users` page.
- The projector page and the remote reconnect by themselves after a connection drop.
- The volume on the remote is the video's volume; the page cannot change the projector's system volume.
- Any logged-in user can control playback, viewers included.

### Watch queue

- The queue button on a video card adds it to the end of the queue. The same video can be queued more than once.
- The queue appears above the list: move entries up and down, remove them, clear the whole queue, or press play to start it on the projector.
- When a video ends, the projector starts the next one from the queue by itself. After a playback error (a network drop, usually) it first retries the same video from where it stopped, and moves on only after three failures in a row. With an empty queue it stops, as before.
- The next button on the remote takes the queue first and falls back to the next video in the library.
- There is one queue for the projector, shared by everyone. It survives a server restart, and deleted videos drop out of it.

## Users and roles

- The built-in administrator has the login `admin`; its password is the value of `PROMIN_TOKEN`.
- The administrator adds other users on the `/users` page. Their passwords are stored in the data volume only as hashes (PBKDF2, 600,000 rounds; a password stored by an older version with fewer is hashed again the next time its owner logs in).
- A **viewer** watches, uploads and deletes videos, changes the encoding settings and controls the projector. An **administrator** can do all of that, also deletes several videos at once, and manages users and paired projectors.
- The video library is shared by everyone.
- Changing a password or deleting a user ends that user's sessions; changing `PROMIN_TOKEN` ends everyone's sessions.
- Logging out revokes the session on the server. A session that has not been used for 30 days ends by itself, and so does the pairing of a projector that has not been in touch for 30 days: it drops out of the list and shows a QR code again. Using the service renews both.
- After five failed login attempts from one address, login is blocked for 15 minutes; a successful login does not cancel earlier failures.

## How videos are processed

- A file that is already 8-bit SDR H.264 and fits the limits from the encoding settings (by default 1920×1080, 60 frames per second and a moderate bitrate) is only repackaged with `+faststart`.
- If the video already fits but the audio does not (not AAC, or more than two channels, such as AC3 5.1), the picture is copied unchanged and only the audio is re-encoded to stereo AAC. This is many times faster and loses no picture quality.
- Everything else is transcoded to H.264 High within those limits, with a bitrate cap, a keyframe every 2 seconds and AAC audio; HDR is converted to SDR.
- The original's metadata (including location) does not reach the finished file.
- The original is deleted after successful processing.
- Each finished video shows its quality: a badge on the thumbnail (SD, 720p, 1080p, 1440p or 4K, with the frame rate from 48 frames per second up, such as 1080p60), the bitrate on the card, and the full details (frame size, frame rate, bitrate, codecs, audio channels) above the player. Videos processed before this was added get it on the next start of the service.

## License

MIT, see [LICENSE](LICENSE).
