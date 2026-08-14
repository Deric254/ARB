const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const BACKEND_DIR = path.join(__dirname, '..', 'backend');
const DIST_DIR = path.join(BACKEND_DIR, 'dist');

console.log('========================================');
console.log('  Building Python Backend');
console.log('========================================\n');

let python = 'python3';
try {
  execSync('python3 --version', { stdio: 'pipe' });
} catch (e) {
  python = 'python';
  try {
    execSync('python --version', { stdio: 'pipe' });
  } catch (e2) {
    console.error('[ERROR] Python not found. Install Python 3.9+');
    process.exit(1);
  }
}

console.log('[+] Python found:', python);

try {
  execSync(python + ' -m PyInstaller --version', { stdio: 'pipe' });
  console.log('[+] PyInstaller found');
} catch (e) {
  console.log('[+] Installing PyInstaller...');
  execSync(python + ' -m pip install pyinstaller', { stdio: 'inherit' });
}

console.log('\n[+] Installing Python dependencies...');
const reqFile = path.join(BACKEND_DIR, 'requirements.txt');
if (fs.existsSync(reqFile)) {
  execSync(python + ' -m pip install -r "' + reqFile + '"', { stdio: 'inherit' });
}

if (fs.existsSync(DIST_DIR)) {
  console.log('[+] Cleaning old build...');
  fs.rmSync(DIST_DIR, { recursive: true, force: true });
}

console.log('\n[+] Bundling Python backend...');
const mainPy = path.join(BACKEND_DIR, 'main.py');
const specContent = `
# -*- mode: python ; coding: utf-8 -*-
import sys
sys.setrecursionlimit(5000)

a = Analysis(
    [r'` + mainPy + `'],
    pathex=[r'` + BACKEND_DIR + `'],
    binaries=[],
    datas=[],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
                   'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
                   'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
                   'uvicorn.lifespan', 'uvicorn.lifespan.on',
                   'fastapi', 'fastapi.middleware', 'fastapi.middleware.cors',
                   'ccxt', 'ccxt.async_support', 'websockets', 'cryptography',
                   'cryptography.fernet', 'sqlite3'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='arb_backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
`;

const specPath = path.join(BACKEND_DIR, 'arb_backend.spec');
fs.writeFileSync(specPath, specContent);

try {
  execSync(python + ' -m PyInstaller "' + specPath + '" --clean --noconfirm', {
    cwd: BACKEND_DIR,
    stdio: 'inherit',
    env: { ...process.env, PYTHONOPTIMIZE: '0' }
  });
  console.log('\n[+] Python backend bundled successfully!');
} catch (e) {
  console.error('\n[!] PyInstaller failed');
  process.exit(1);
}
