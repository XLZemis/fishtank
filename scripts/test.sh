#!/usr/bin/env bash
# 一键检查：Python/Node 语法 + 单元测试 + git whitespace。
#
# 测试需要 NoneBot 运行环境；默认在部署机 your-server 的 qqbot-nonebot 容器里跑。
# 用法：
#   ./scripts/test.sh                    # 本机有 docker 就直接跑，否则 ssh your-server
#   DEPLOY_HOST=myhost ./scripts/test.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[1/4] Python 语法"
python3 -m compileall -q perception persona/tools scripts

echo "[2/4] Node 语法"
node --check bridge/server.mjs
for f in persona/plugins/*/lib/index.js; do
  [ -e "$f" ] || continue
  node --check "$f"
done

echo "[3/4] 单元测试"
if docker info >/dev/null 2>&1 && docker inspect qqbot-nonebot >/dev/null 2>&1; then
  docker cp tests qqbot-nonebot:/tmp/fishtank-tests >/dev/null
  docker cp persona/tools/doc2text.py qqbot-nonebot:/tmp/fishtank-tests/doc2text.py >/dev/null
  docker exec -e PERCEPTION_PARENT=/app/app/plugins qqbot-nonebot \
    python3 -m unittest discover -s /tmp/fishtank-tests -p 'test_*.py' -v
else
  HOST="${DEPLOY_HOST:-}"
  if [ -z "$HOST" ]; then
    echo "  本机没有 qqbot-nonebot 容器；请在本机 Docker 或设置 DEPLOY_HOST" >&2
    exit 1
  fi
  echo "  本机没有 Docker，改用 ssh ${HOST} 的 qqbot-nonebot 容器"
  rsync -a --delete tests/ "${HOST}:/tmp/fishtank-tests/" >/dev/null
  rsync -q persona/tools/doc2text.py "${HOST}:/tmp/fishtank-tests/doc2text.py" >/dev/null
  ssh "$HOST" 'docker cp /tmp/fishtank-tests qqbot-nonebot:/tmp/ >/dev/null; \
    docker exec -e PERCEPTION_PARENT=/app/app/plugins qqbot-nonebot \
    python3 -m unittest discover -s /tmp/fishtank-tests -p "test_*.py" -v'
fi

echo "[4/4] git whitespace 检查"
git diff --check

echo "全部通过。"
