// What the page knows right now. Every part reads it; the parts that change a value say so.

export var state = {
  maxUpload: Infinity,
  isAdmin: false,
  canManage: false,     // a person with a login, not a paired projector
  selecting: false,     // administrators pick several videos to delete at once
  selected: {},         // id → true
  videos: [],           // the library, as the server last sent it
  folders: [],          // [{id, name, count}]
  currentFolder: 'all', // 'all', 'none' or a folder id; remembered on this phone
  queue: [],            // what the projector plays next; the server sends it over the WebSocket
  tvConnected: false,
  tvState: null         // what the projector is playing, as it last reported
};
