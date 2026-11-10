#!/usr/bin/env bash
# 发布：每次新建一个「孤立单提交」并强推 gh-pages。
# 旧提交立即变成不可达对象，由 GitHub 侧 GC 回收 —— 仓库体积恒定，不需要人工清理历史。
set -euo pipefail

cd "$GITHUB_WORKSPACE"
: "${GITHUB_TOKEN:?需要 GITHUB_TOKEN}"
: "${GITHUB_REPOSITORY:?需要 GITHUB_REPOSITORY}"

if [ ! -f dist/index.html ]; then
  echo "dist/index.html 不存在，拒绝发布" >&2
  exit 1
fi

rm -rf .deploy
mkdir -p .deploy
cp -a dist/. .deploy/
touch .deploy/.nojekyll
# 产物里绝不允许出现密钥/缓存/数据库等运行垃圾
find .deploy -type f \( -name '*.pyc' -o -name '*.log' -o -name '*.sqlite' \
  -o -name '*.tmp' -o -name '*.bak' \) -delete

cd .deploy
git init -q -b gh-pages
git config user.name "qdii-bot"
git config user.email "qdii-bot@users.noreply.github.com"
git add -A
STAMP="$(date -u '+%Y-%m-%d %H:%M UTC')"
git commit -q -m "QDII 快照 ${STAMP}（孤立提交，仓库体积恒定）"
git remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
git push -q --force origin gh-pages
echo "已发布 gh-pages：$(git rev-parse --short HEAD)，共 $(git ls-files | wc -l) 个文件，$(du -sh . | cut -f1)"
