#!/usr/bin/env bash
# =============================================================================
#  手动拉起 PP-OCRv5 OCR 服务（FastAPI + PaddleOCR，CPU，端口 8118）
#
#  约定：本服务【不再配置任何开机自启 / 自动拉起】，需要时手动运行本脚本。
#  运行位置：服务器上本仓库目录（部署副本通常在 /root/local_PP_OCRv5_server）
#
#  用法：
#    bash start_service_cpu.sh      # 启动（幂等：已在运行则直接退出）
#
#  停止 / 查看日志：
#    podman-compose -f podman-compose-cpu.yml down        # 在仓库目录执行（停止并删除容器）
#    podman-compose -f podman-compose-cpu.yml logs -f     # 跟踪日志
# =============================================================================
set -euo pipefail

# --- 常量 ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODEL_DIR="./models"
COMPOSE_FILE="podman-compose-cpu.yml"
SERVICE_URL="http://127.0.0.1:8118/"
WAIT_LOOPS=90            # 每 2 秒一次，共约 180 秒（首次加载模型较慢）
WAIT_INTERVAL=2

log() { printf '\033[1;32m[start]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[start]\033[0m %s\n' "$*" >&2; }

# --- 1. 基础环境检查 ---
command -v podman        >/dev/null 2>&1 || { err "未找到 podman（请在服务器上运行本脚本）"; exit 1; }
command -v podman-compose >/dev/null 2>&1 || { err "未找到 podman-compose"; exit 1; }
[ -f "$COMPOSE_FILE" ] || { err "找不到 $COMPOSE_FILE，请在仓库目录内运行本脚本"; exit 1; }
if [ "$(id -u)" -ne 0 ]; then
    log "提示：当前非 root 用户；若容器是 rootful（root 创建）将不可见，请改用 root 运行。"
fi

# --- 2. 模型目录检查 ---
if [ ! -d "$MODEL_DIR/PaddlePaddle/PP-OCRv5_server_det" ] || \
   [ ! -d "$MODEL_DIR/PaddlePaddle/PP-OCRv5_server_rec" ]; then
    err "模型目录不完整：$MODEL_DIR/PaddlePaddle/ 下缺少 PP-OCRv5_server_det 或 PP-OCRv5_server_rec"
    exit 1
fi
log "模型文件齐全。"

# --- 3. 已在运行则幂等退出；有旧容器则先清理（用 compose 重建） ---
if podman container exists ppocr_v5_server_cpu 2>/dev/null; then
    state="$(podman inspect ppocr_v5_server_cpu --format '{{.State.Status}}' 2>/dev/null || echo unknown)"
    if [ "$state" = "running" ]; then
        log "容器 ppocr_v5_server_cpu 已在运行（$SERVICE_URL），无需重复启动。"
        log "如需重启：先 podman-compose -f $COMPOSE_FILE down，再运行本脚本。"
        exit 0
    fi
    log "存在旧容器（状态: $state），先 podman-compose -f $COMPOSE_FILE down 清理……"
    podman-compose -f "$COMPOSE_FILE" down || err "podman-compose down 失败（忽略，继续尝试 up）"
fi

# --- 4. 拉起服务 ---
log "执行 podman-compose -f $COMPOSE_FILE up -d ……"
podman-compose -f "$COMPOSE_FILE" up -d

# --- 5. 等待就绪（GET / 返回任意 HTTP 状态码即代表 uvicorn 已监听、模型加载完成） ---
log "等待服务就绪（最长 $((WAIT_LOOPS * WAIT_INTERVAL)) 秒，首次需加载模型）……"
for ((i = 1; i <= WAIT_LOOPS; i++)); do
    if curl -s -o /dev/null -m 2 "$SERVICE_URL"; then
        status="$(podman ps --filter name=ppocr_v5_server_cpu --format '{{.Status}}' 2>/dev/null || echo '?')"
        log "服务已就绪: $SERVICE_URL  （容器状态: $status）"
        log "OCR 接口: POST $SERVICE_URL 之后按 /ocr 协议提交 base64 图片"
        log "说明: GET / 返回 404 属正常（该路径无路由）"
        exit 0
    fi
    sleep "$WAIT_INTERVAL"
done

err "❌ $((WAIT_LOOPS * WAIT_INTERVAL)) 秒内服务未就绪，请查看日志排查："
err "   podman-compose -f $COMPOSE_FILE logs -f    或    podman logs ppocr_v5_server_cpu"
exit 1
