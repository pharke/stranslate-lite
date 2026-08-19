#!/usr/bin/env bash
# 构建最小 .app 启动器：菜单栏常驻「译」，无 Dock 图标、无终端窗口。
# 产物默认放在 ~/Applications/stranslate-lite.app（Spotlight/启动台可见），
# 可拖入「应用程序」或「系统设置 → 登录项」实现开机自启。
#
# 用法：
#   scripts/build_app.sh                       # 用默认 ~/.venvs/stranslate-lite
#   scripts/build_app.sh /path/to/venv /path/out.app
set -euo pipefail

VENV="${1:-$HOME/.venvs/stranslate-lite}"
APP_DIR="${2:-$HOME/Applications/stranslate-lite.app}"
VERSION="${3:-0.2.0}"

if [[ ! -x "$VENV/bin/stranslate-lite" ]]; then
    echo "未找到 $VENV/bin/stranslate-lite，请先在该 venv 中执行 pip install -e ." >&2
    exit 1
fi

mkdir -p "$APP_DIR/Contents/MacOS"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>stranslate-lite</string>
  <key>CFBundleDisplayName</key><string>stranslate-lite</string>
  <key>CFBundleIdentifier</key><string>com.user.stranslate-lite</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>stranslate-lite</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP_DIR/Contents/MacOS/stranslate-lite" <<EXEC
#!/bin/bash
exec "$VENV/bin/stranslate-lite" run >> "\$HOME/Library/Logs/stranslate-lite.log" 2>&1
EXEC
chmod +x "$APP_DIR/Contents/MacOS/stranslate-lite"

# 临时签名（本地自建 .app 需要签名以避免 Gatekeeper 拦报；非签名也可运行，
# 但签名后 TCC 授权更稳定地归属到本 app）
if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "已生成：$APP_DIR"
echo "启动：open \"$APP_DIR\"（或 Spotlight 搜 stranslate-lite）"
