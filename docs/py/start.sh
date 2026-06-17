#!/bin/bash
cd "$(dirname "$0")"

echo "=== 企业知识库 Agent · 平台启动器 ==="
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] 需要 Python 3.10+"
    exit 1
fi

# Install deps if needed
if [ ! -d "venv" ]; then
    echo "[1/4] 创建虚拟环境..."
    python3 -m venv venv
fi

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

source venv/bin/activate 2>/dev/null || source venv/Scripts/activate 2>/dev/null

PYTHON_BIN="$(pwd)/venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python"
fi

PORT_VALUE="${PORT:-6090}"

echo "[2/4] 安装依赖..."
"$PYTHON_BIN" -m pip install -r requirements.txt -q

echo "[3/4] 初始化数据库..."
"$PYTHON_BIN" generate_keys.py

if [ -z "${DASHSCOPE_API_KEYS:-}" ] && [ -z "${DASHSCOPE_API_KEY:-}" ] && [ ! -f "config/api_keys.txt" ]; then
    echo "[ERROR] 未检测到模型 API Key。"
    echo "请先配置以下任一方式后再启动："
    echo "  1. 编辑 config/api_keys.txt"
    echo "  2. 或在 .env 中设置 DASHSCOPE_API_KEYS"
    exit 1
fi

echo "[4/4] 启动服务..."
echo ""
echo "  H5前端:    http://localhost:${PORT_VALUE}"
echo "  管理后台:  http://localhost:${PORT_VALUE}/admin"
echo "  管理账号:  默认管理员账号请查看部署文档，并在首次启动后及时修改密码"
echo "  Key配置:   config/api_keys.txt 或 .env"
echo "  Python环境: $PYTHON_BIN"
echo "  监听端口:   ${PORT_VALUE}"
echo ""
"$PYTHON_BIN" -m uvicorn backend.main:app --host 0.0.0.0 --port "${PORT_VALUE}"
