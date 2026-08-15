const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('========================================');
console.log('  ARB Pro Desktop Builder v6.0');
console.log('========================================\n');

// Step 1: Generate icons from logo.png (or a branded placeholder if none provided)
console.log('[Step 1/4] Generating icons...');
const logoPath = path.join(__dirname, '..', 'logo.png');
const buildDir = path.join(__dirname, '..', 'build');
if (!fs.existsSync(buildDir)) fs.mkdirSync(buildDir, { recursive: true });

const python = process.platform === 'win32' ? 'python' : 'python3';
const hasLogo = fs.existsSync(logoPath);

const script = `
import sys
from PIL import Image, ImageDraw
import os

out = sys.argv[1]
has_logo = len(sys.argv) > 2 and sys.argv[2] != ''

if has_logo:
    img = Image.open(sys.argv[2]).convert('RGBA')
else:
    # Branded placeholder so the build never breaks just because no
    # custom logo.png was supplied. Matches the app's green accent (#00ff88).
    size = 512
    img = Image.new('RGBA', (size, size), (10, 10, 10, 255))
    d = ImageDraw.Draw(img)
    d.ellipse([40, 40, size - 40, size - 40], fill=(0, 255, 136, 255))
    d.ellipse([100, 100, size - 100, size - 100], fill=(10, 10, 10, 255))

sizes = [(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)]
img.save(os.path.join(out, 'icon.ico'), format='ICO', sizes=sizes)

img.resize((256,256), Image.LANCZOS).save(os.path.join(out, 'icon.png'))
img.resize((512,512), Image.LANCZOS).save(os.path.join(out, 'icon-512.png'))
print('Icons generated' + (' from logo.png' if has_logo else ' (placeholder — no logo.png found)'))
`;
const tmpPy = path.join(__dirname, '..', '_tmp_icon.py');
try {
  fs.writeFileSync(tmpPy, script);
  const args = hasLogo ? `"${buildDir}" "${logoPath}"` : `"${buildDir}"`;
  execSync(`${python} "${tmpPy}" ${args}`, { stdio: 'inherit' });
  fs.unlinkSync(tmpPy);
  if (hasLogo) {
    console.log('    Icons created from logo.png\n');
  } else {
    console.log('    [!] No logo.png found — used a placeholder icon so the build can proceed.');
    console.log('        Tip: add logo.png to the project root and re-run for custom branding.\n');
  }
} catch (e) {
  console.error('    [!] Icon generation failed: ' + e.message);
  console.error('        This is fatal — electron-builder requires build/icon.ico to exist.');
  console.error('        Make sure Python and Pillow are installed (pip install Pillow).');
  if (fs.existsSync(tmpPy)) fs.unlinkSync(tmpPy);
  process.exit(1);
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
  console.error('[!] Python backend build failed.');
  console.error('    This is fatal for a packaged build — the installer would ship');
  console.error('    with no backend inside it. Fix the PyInstaller error above and retry.');
  process.exit(1);
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
