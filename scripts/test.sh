#!/usr/bin/env bash
# 一键检查：Python/Node 语法 + 桥策略单测 + 感知层单测 + git whitespace。
#
# 重要（2026-10-25 起）：单测加载的是**当前工作区的 perception 代码**
# （复制到容器的 /tmp/fishtank-code），不是容器里已部署的旧版本——先测再上线。
#
# 用法：
#   ./scripts/test.sh                    # 本机有 docker 就直接跑，否则 ssh your-server
#   DEPLOY_HOST=myhost ./scripts/test.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[1/5] Python 语法"
python3 -m compileall -q perception persona/tools scripts

echo "[2/5] Node 语法 + 桥策略单测"
node --check bridge/server.mjs
node --check bridge/netguard.mjs
for f in persona/plugins/*/lib/index.js; do
  [ -e "$f" ] || continue
  node --check "$f"
done
node --test bridge/netguard.test.mjs

echo "[3/5] 单元测试（当前工作区的 perception）"
if docker info >/dev/null 2>&1 && docker inspect qqbot-nonebot >/dev/null 2>&1; then
  docker exec qqbot-nonebot mkdir -p /tmp/fishtank-code/perception /tmp/fishtank-tests
  docker cp perception/. qqbot-nonebot:/tmp/fishtank-code/perception
  docker cp tests/. qqbot-nonebot:/tmp/fishtank-tests
  docker cp persona/tools/doc2text.py qqbot-nonebot:/tmp/fishtank-tests/doc2text.py >/dev/null
  docker exec -e PERCEPTION_PARENT=/tmp/fishtank-code qqbot-nonebot     python3 -m unittest discover -s /tmp/fishtank-tests -p 'test_*.py' -v
else
  HOST="${DEPLOY_HOST:-your-server}"
  echo "  本机没有 Docker，改用 ssh ${HOST} 的 qqbot-nonebot 容器"
  ssh "$HOST" 'mkdir -p /tmp/fishtank-code/perception /tmp/fishtank-tests'
  rsync -a --delete perception/ "${HOST}:/tmp/fishtank-code/perception/" >/dev/null
  rsync -a --delete tests/ "${HOST}:/tmp/fishtank-tests/" >/dev/null
  rsync -q persona/tools/doc2text.py "${HOST}:/tmp/fishtank-tests/doc2text.py" >/dev/null
  ssh "$HOST" 'docker exec qqbot-nonebot mkdir -p /tmp/fishtank-code/perception /tmp/fishtank-tests; \
    docker cp /tmp/fishtank-code/perception/. qqbot-nonebot:/tmp/fishtank-code/perception >/dev/null; \
    docker cp /tmp/fishtank-tests/. qqbot-nonebot:/tmp/fishtank-tests >/dev/null; \
    docker exec -e PERCEPTION_PARENT=/tmp/fishtank-code qqbot-nonebot \
    python3 -m unittest discover -s /tmp/fishtank-tests -p "test_*.py" -v'
fi

echo "[4/5] git whitespace 检查"
git diff --check

echo "[5/5] 全部通过。"
