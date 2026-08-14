const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  openSettings: () => ipcRenderer.invoke('open-settings'),
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  selectLogo: () => ipcRenderer.invoke('select-logo'),
  getBackendStatus: () => ipcRenderer.invoke('get-backend-status'),
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  onBackendDisconnected: (callback) => {
    ipcRenderer.on('backend-disconnected', () => callback());
  }
});
