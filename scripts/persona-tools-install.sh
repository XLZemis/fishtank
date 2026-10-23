#!/usr/bin/env bash
# 给人格容器安装文档解析/基础 CLI，并提交成镜像（重建容器后仍保留）。
#
# 用法（在 your-server 宿主机、仓库根目录执行）：
#   ./scripts/persona-tools-install.sh [容器名] [镜像tag]
# 默认：dsh-persona / dsh-persona-tools:YYYYMMDD
#
# 注意：脚本会 apt-get/pip 安装（需要网络），并在容器里写入 /usr/local/bin/doc2text；
# 最后 docker commit 成镜像。随后把 /data/persona/compose.yaml 的 image 改成该 tag
# 并 docker compose up -d，容器重建后工具仍在。
set -euo pipefail

CONTAINER="${1:-dsh-persona}"
TAG="${2:-dsh-persona-tools:$(date +%Y%m%d)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[1/4] apt 安装基础工具..."
docker exec "$CONTAINER" sh -lc '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    python3 python3-pip python3-venv poppler-utils pandoc catdoc antiword unrtf \
    git curl wget jq zip unzip file less tree ffmpeg tesseract-ocr tesseract-ocr-chi-sim \
    imagemagick sqlite3
'

echo "[2/4] pip 安装文档解析库..."
docker exec "$CONTAINER" pip3 install --no-cache-dir --break-system-packages \
  pypdf pdfplumber PyMuPDF python-docx openpyxl python-pptx beautifulsoup4 lxml odfpy markdownify

echo "[3/4] 安装 doc2text..."
docker exec -i "$CONTAINER" sh -lc 'cat > /usr/local/bin/doc2text && chmod +x /usr/local/bin/doc2text' \
  < "$ROOT/persona/tools/doc2text.py"

echo "[4/4] 提交镜像 $TAG ..."
docker commit "$CONTAINER" "$TAG"
echo "完成。把 compose 的 image 改为 $TAG 后 docker compose up -d。"
