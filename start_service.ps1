# =============================================================================
#  手动拉起 PP-OCRv5 OCR 服务（FastAPI + PaddleOCR，GPU，端口 8118）
#
#  Windows + Docker Desktop 版，对应 Linux 上的 start_service.sh / podman-compose。
#  约定：本服务【不做开机自启 / 自动拉起】，需要时手动运行本脚本。
#
#  用法：
#    .\start_service.ps1          # 启动（幂等：已在运行则直接退出）
#
#  停止 / 查看日志（在仓库目录执行）：
#    docker compose down
#    docker compose logs -f
# =============================================================================
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$ModelDir = Join-Path $ScriptDir "models"
$ComposeFile = "docker-compose.yml"
$ContainerName = "ppocr_v5_server"
$ImageName = "ppocr_v5_server:latest"
$ServiceUrl = "http://127.0.0.1:8118/"
$WaitLoops = 90
$WaitIntervalSec = 2

function Write-StartLog([string]$Message) {
    Write-Host "[start] $Message" -ForegroundColor Green
}
function Write-StartErr([string]$Message) {
    Write-Host "[start] $Message" -ForegroundColor Red
}

# --- 1. 基础环境检查 ---
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-StartErr "未找到 docker（请先安装并启动 Docker Desktop）"
    exit 1
}
$ErrorActionPreference = "Continue"
docker info 1>$null 2>$null
$dockerOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $dockerOk) {
    Write-StartErr "Docker 未就绪（Docker Desktop 是否已启动？）"
    exit 1
}
if (-not (Test-Path (Join-Path $ScriptDir $ComposeFile))) {
    Write-StartErr "找不到 $ComposeFile，请在仓库目录内运行本脚本"
    exit 1
}

# --- 2. 模型目录检查（./models） ---
$det = Join-Path $ModelDir "PaddlePaddle\PP-OCRv5_server_det"
$rec = Join-Path $ModelDir "PaddlePaddle\PP-OCRv5_server_rec"
if (-not ((Test-Path $det) -and (Test-Path $rec))) {
    Write-StartErr "模型目录不完整：$ModelDir\PaddlePaddle\ 下缺少 PP-OCRv5_server_det 或 PP-OCRv5_server_rec"
    exit 1
}
Write-StartLog "模型目录就绪: $ModelDir"

# --- 3. 镜像标签（迁移自 Podman 时常带 localhost/ 前缀） ---
$hasImage = docker images -q $ImageName 2>$null
if (-not $hasImage) {
    $localhostId = docker images -q "localhost/ppocr_v5_server:latest" 2>$null
    if ($localhostId) {
        Write-StartLog "将 localhost/ppocr_v5_server:latest 标记为 $ImageName ..."
        docker tag "localhost/ppocr_v5_server:latest" $ImageName
    } else {
        Write-StartErr "找不到镜像 $ImageName（也没有 localhost/ppocr_v5_server:latest）"
        exit 1
    }
}

# --- 4. 已在运行则幂等退出；有旧容器则先清理 ---
$existing = docker ps -a --filter "name=^/${ContainerName}$" --format "{{.ID}}" 2>$null
if ($existing) {
    $state = (docker inspect $ContainerName --format "{{.State.Status}}" 2>$null)
    if ($state -eq "running") {
        Write-StartLog "容器 $ContainerName 已在运行（$ServiceUrl），无需重复启动。"
        Write-StartLog "如需重启：先 docker compose down，再运行本脚本。"
        exit 0
    }
    Write-StartLog "存在旧容器（状态: $state），先 docker compose down 清理..."
    $ErrorActionPreference = "Continue"
    docker compose -f $ComposeFile down
    if ($LASTEXITCODE -ne 0) {
        Write-StartErr "docker compose down 失败（忽略，继续尝试 up）"
    }
    $ErrorActionPreference = "Stop"
}

# --- 5. 拉起服务 ---
Write-StartLog "执行 docker compose up -d ..."
docker compose -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) {
    Write-StartErr "docker compose up -d 失败"
    exit 1
}

# --- 6. 等待就绪 ---
$maxWait = $WaitLoops * $WaitIntervalSec
Write-StartLog "等待服务就绪（最长 ${maxWait} 秒，首次需加载模型）..."
for ($i = 1; $i -le $WaitLoops; $i++) {
    $ready = $false
    try {
        $null = Invoke-WebRequest -Uri $ServiceUrl -UseBasicParsing -TimeoutSec 2
        $ready = $true
    } catch {
        # 404/任意 HTTP 响应都说明 uvicorn 已起来；连不上才继续等
        if ($_.Exception.Response) {
            $ready = $true
        }
    }
    if ($ready) {
        $status = (docker ps --filter "name=$ContainerName" --format "{{.Status}}" 2>$null)
        if (-not $status) { $status = "?" }
        Write-StartLog "服务已就绪: $ServiceUrl  （容器状态: $status）"
        Write-StartLog "OCR 接口: POST http://127.0.0.1:8118/ocr"
        Write-StartLog "说明: GET / 返回 404 属正常（该路径无路由）"
        exit 0
    }
    Start-Sleep -Seconds $WaitIntervalSec
}

Write-StartErr "${maxWait} 秒内服务未就绪，请查看日志排查："
Write-StartErr "   docker compose logs -f"
Write-StartErr "   docker logs $ContainerName"
exit 1
