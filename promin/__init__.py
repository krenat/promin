"""Promin, a home video server for a projector.

Video list, upload, transcoding, playback, deletion, users with roles and a remote:
the phone controls the projector page over WebSocket. The projector is paired with
a QR code from its screen that is scanned and approved on the phone.

Where things are:

- config: settings from the environment and the paths of the project;
- db: the SQLite database every store reads and writes;
- legacy: bringing over the JSON files of older versions;
- upload: what may be uploaded, and writing it to disk;
- library: the video list, the folders and the watch queue;
- encoding: the encoding settings;
- ffmpeg, transcode: what to ask of ffmpeg, and the worker that runs it;
- accounts, devices: users and their sessions; paired projectors;
- auth, crypto: telling who is asking by the cookie; the login limit; hashing;
- assets: the styles and scripts, named under a version so browsers do not mix them;
- hub: the live connections of the projector page and the phones;
- playback: the watch queue as phones see it, and what plays next;
- parsing: reading values that arrive from a page;
- tvtest: the projector check;
- services: all of the above for one running app, and what spans several of them;
- routes: the pages and the API, one module per subject;
- app: create_app, which puts it together.
"""
