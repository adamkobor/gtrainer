#!/usr/bin/env bash
# Builds chiaki-ng with the contrainer external-input patch for this Mac (Apple Silicon).
#
#   chiaki/build.sh            # clone (if needed), patch, build
#   chiaki/build.sh --deps     # also install Homebrew build dependencies first
#
# Result: chiaki/src/build/gui/chiaki.app (runs against the Homebrew libs; not a
# redistributable bundle). Steps mirror upstream .github/workflows/build-macos-arm.yml.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src"
UPSTREAM="https://github.com/streetpea/chiaki-ng.git"
# Commit the patch was written against; bump deliberately and re-test.
PINNED_COMMIT="43728af2e639fecb8a7af1cd68c29820a40de973"
PATCH="$HERE/external-input.patch"

if [[ "${1:-}" == "--deps" ]]; then
  brew install qt@6 ffmpeg pkgconf opus openssl@3 cmake ninja nasm protobuf@29 \
    speexdsp libplacebo json-c miniupnpc libevent sdl2-compat
  python3 -m pip install --user 'protobuf>=5,<6' --break-system-packages
fi

if [[ ! -d "$SRC/.git" ]]; then
  git clone --filter=blob:none "$UPSTREAM" "$SRC"
fi

cd "$SRC"
if ! git diff --quiet HEAD -- || [[ -n "$(git ls-files --others --exclude-standard gui)" ]]; then
  echo "chiaki/src has local changes; checking whether the patch is already applied..."
  if git apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "patch already applied"
  else
    echo "error: chiaki/src is modified and the patch does not match; clean it first" >&2
    exit 1
  fi
else
  git fetch --quiet origin "$PINNED_COMMIT"
  git checkout --quiet "$PINNED_COMMIT"
  git submodule update --init --recursive
  git apply "$PATCH"
  echo "patch applied on $PINNED_COMMIT"
fi

BREW="$(brew --prefix)"
cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCHIAKI_ENABLE_CLI=OFF \
  -DCHIAKI_ENABLE_STEAMDECK_NATIVE=OFF \
  -DOPENSSL_ROOT_DIR="$BREW/opt/openssl@3" \
  -DOPENSSL_INCLUDE_DIR="$BREW/opt/openssl@3/include" \
  -DOPENSSL_CRYPTO_LIBRARY="$BREW/opt/openssl@3/lib/libcrypto.dylib" \
  -DOPENSSL_SSL_LIBRARY="$BREW/opt/openssl@3/lib/libssl.dylib" \
  `# Opus is otherwise found as $BREW/include, which puts openssl@4's headers` \
  `# ahead of openssl@3 (link error: _OPENSSL_sk_set_*_thunks). Upstream CI` \
  `# instead runs "brew unlink openssl@4"; this avoids changing the system.` \
  -DOpus_INCLUDE_DIRS="$BREW/opt/opus/include" \
  -DCMAKE_PREFIX_PATH="$BREW/opt/openssl@3;$BREW/opt/qt@6;$BREW/opt/protobuf@29"

export CPATH="$BREW/opt/ffmpeg/include"
cmake --build build --config Release --target chiaki

APP="$SRC/build/gui/chiaki.app"
# macOS Local Network privacy: without a consistently signed bundle (the linker only
# ad-hoc signs the binary) the app's UDP discovery/registration packets are silently
# dropped ("Regist search failed"). Add a usage string and sign the whole bundle
# like upstream's release workflow does.
/usr/libexec/PlistBuddy -c "Delete :NSLocalNetworkUsageDescription" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSLocalNetworkUsageDescription string 'chiaki-ng finds and connects to your PlayStation on the local network.'" "$APP/Contents/Info.plist"
codesign --force --deep --sign - --entitlements "$SRC/gui/entitlements.xml" "$APP"

echo
echo "Built: $APP"
echo "Run:   open \"$SRC/build/gui/chiaki.app\"   (external input listens on 127.0.0.1:47800)"
