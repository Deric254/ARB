const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  selectLogo: () => ipcRenderer.invoke('select-logo'),
  getBackendStatus: () => ipcRenderer.invoke('get-backend-status'),
  getApiToken: () => ipcRenderer.invoke('get-api-token'),
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  onBackendDisconnected: (callback) => {
    ipcRenderer.on('backend-disconnected', () => callback());
  },
  onNavigateSettings: (callback) => {
    ipcRenderer.on('navigate-settings', () => callback());
  }
});
