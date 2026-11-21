'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const SEND_CHANNELS = new Set([
  'window:minimize',
  'window:maximize',
  'window:close',
  'app:quit',
  'overlay:resize',
]);

const ON_CHANNELS = new Set(['window:state']);

contextBridge.exposeInMainWorld('kivi', {
  send(channel, payload) {
    if (!SEND_CHANNELS.has(channel)) return false;
    ipcRenderer.send(channel, payload);
    return true;
  },
  on(channel, callback) {
    if (!ON_CHANNELS.has(channel) || typeof callback !== 'function') {
      return () => {};
    }
    const listener = (_event, data) => callback(data);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  },
});
