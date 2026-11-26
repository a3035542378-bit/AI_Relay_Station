#!/bin/sh
# 在全新解压的 Open WebUI 0.13.5 源码目录中构建个人连接设置。
set -eu
if [ "$#" -ne 1 ]; then
    echo "用法: sh deploy/build_frontend.sh <Open WebUI 源码目录>" >&2
    exit 2
fi
patch_path="$(cd "$(dirname "$0")" && pwd)/personal-settings.patch"
cd "$1"
node -e "if (require('./package.json').version !== '0.13.5' || process.versions.node.split('.')[0] !== '22') process.exit(1)"
git apply --check "$patch_path"
git apply "$patch_path"
CYPRESS_INSTALL_BINARY=0 npm ci --no-audit --no-fund
npm_package_version=0.13.5 node --max-old-space-size=7168 node_modules/vite/bin/vite.js build --sourcemap false
