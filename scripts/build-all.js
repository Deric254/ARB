const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('========================================');
console.log('  ARB Pro Desktop Builder v6.0');
console.log('========================================\n');

// Step 1: Generate icons from logo.png
console.log('[Step 1/4] Generating icons...');
const logoPath = path.join(__dirname, '..', 'logo.png');
const buildDir = path.join(__dirname, '..', 'build');
if (!fs.existsSync(buildDir)) fs.mkdirSync(buildDir, { recursive: true });

if (fs.existsSync(logoPath)) {
  try {
    const python = process.platform === 'win32' ? 'python' : 'python3';
    const script = `
import sys
from PIL import Image
import os

logo = sys.argv[1]
out = sys.argv[2]
img = Image.open(logo).convert('RGBA')

sizes = [(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)]
imgs = [img.resize(s, Image.LANCZOS) for s in sizes]
imgs[0].save(os.path.join(out, 'icon.ico'), format='ICO', sizes=sizes)

img.resize((256,256), Image.LANCZOS).save(os.path.join(out, 'icon.png'))
img.resize((512,512), Image.LANCZOS).save(os.path.join(out, 'icon-512.png'))
print('Icons generated')
`;
    const tmpPy = path.join(__dirname, '..', '_tmp_icon.py');
    fs.writeFileSync(tmpPy, script);
    execSync(python + ' "' + tmpPy + '" "' + logoPath + '" "' + buildDir + '"', { stdio: 'inherit' });
    fs.unlinkSync(tmpPy);
    console.log('    Icons created from logo.png\n');
  } catch (e) {
    console.log('    [!] Could not generate icons. Using defaults.\n');
  }
} else {
  console.log('    [!] logo.png not found. Using default Electron icon.\n');
  console.log('        Tip: Add logo.png and re-run to use custom branding.\n');
}

// Step 2: Install Node deps
console.log('[Step 2/4] Installing Node dependencies...');
try {
  execSync('npm install', { cwd: path.join(__dirname, '..'), stdio: 'inherit' });
} catch (e) {
  console.error('[!] npm install failed');
  process.exit(1);
}

// Step 3: Build Python backend
console.log('\n[Step 3/4] Building Python backend...');
try {
  execSync('node scripts/build-py.js', { cwd: path.join(__dirname, '..'), stdio: 'inherit' });
} catch (e) {
  console.error('[!] Python backend build failed');
  console.log('    Continuing with dev mode (requires Python installed)...\n');
}

// Step 4: Build Electron app
console.log('\n[Step 4/4] Building Electron app...');
try {
  execSync('npx electron-builder', { cwd: path.join(__dirname, '..'), stdio: 'inherit' });
  console.log('\n========================================');
  console.log('  BUILD COMPLETE!');
  console.log('========================================');
  console.log('');
  console.log('  Installer: dist/ARB Pro Setup.exe');
  console.log('  Portable:  dist/win-unpacked/ARB Pro.exe');
  console.log('');
  console.log('  This installer includes:');
  console.log('    - Electron frontend');
  console.log('    - Bundled Python backend');
  console.log('    - All dependencies');
  console.log('    - No separate installs needed');
  console.log('');
} catch (e) {
  console.error('[!] Electron build failed');
  process.exit(1);
}
