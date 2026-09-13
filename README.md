# local_PP_OCRv5_server

PP-OCRv5 文本**识别**服务的本地部署仓库：跑一个 GPU 版 OCR HTTP 服务，并配套「数据预处理」与「识别评测」两个脚本。
不做检测定位（图片已由 ui-annotator 按标注框裁好）。

```
ui-annotator(GUI) ──导出──> D:\ui-dataset\ocr_export
                                   │
                    [本机] prepare_rec_dataset.py  ← 预处理
                                   ▼
                    项目根 train_data/rec/（train/ test/ rec_gt_*.txt）
                                   │
                            同步到训练机 → PPOCR 训练

[本机] eval_rec_service.py ──POST /ocr──> [服务器 192.168.50.2] ppocr_v5_server 容器（GPU · 8118）
```

---

## 0. 环境速览

| 角色 | 机器 / 地址 | 关键路径 |
|---|---|---|
| OCR 服务（GPU） | 服务器 `192.168.50.2`（hostname `k8s-master`），SSH 别名 `localserver` | 部署目录 `/mnt/pssd/remote-project/local_PP_OCRv5_server`；模型 `/mnt/pssd/.../models/PaddlePaddle/PP-OCRv5_server_{det,rec}` |
| 预处理 / 评测 | **本开发机（WSL）** | 本仓库根目录；数据盘 `/mnt/d/ui-dataset`；Python 3.14（脚本只用标准库，无需 venv） |
| 标注 / 导出 | Windows 上的 ui-annotator | `/home/listone/game-automation/ui-annotator`，数据集 `D:\ui-dataset` |

> 前提：服务器与开发机之间是网线直连（`192.168.50.x`）。SSH 用 `ssh -F ~/.ssh/config localserver`（系统 `/etc/ssh/ssh_config.d` 损坏，必须带 `-F`）。

---

## 1. 拉起 / 停止 OCR 服务（在服务器上执行）

服务**没有任何开机自启 / 自动拉起**，服务器重启后需要手动拉起。

### 1.1 启动

```bash
ssh -F ~/.ssh/config localserver
cd /mnt/pssd/remote-project/local_PP_OCRv5_server
bash start_service.sh
```

- 幂等：已在运行会直接退出；
- 会先检查模型盘 `/mnt/pssd` 是否挂载、模型是否齐全，刷新 NVIDIA CDI，然后 `podman-compose up -d`；
- 最长等 180 秒（首次加载模型慢），成功后打印「服务已就绪」。

### 1.2 确认就绪（在开发机上执行）

```bash
# 容器状态
ssh -F ~/.ssh/config localserver 'podman ps --format "{{.Names}} | {{.Status}}"'

# 接口是否在监听：返回 404 是正常的（GET / 无路由），连不上才是没起来
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.50.2:8118/
```

### 1.3 停止 / 看日志（在服务器仓库目录执行）

```bash
podman-compose down      # 停止并删除容器
podman-compose logs -f   # 跟踪日志
```

### 1.4 接口速查

| 项 | 值 |
|---|---|
| 接口 | `POST http://192.168.50.2:8118/ocr` |
| 请求 | `{"image": "<图片Base64，不带 data: 前缀>", "image_type": "png"}` |
| 响应 | `{"code":0,"message":"success","data":{"input_path":null,"words":[{"text":"武将","confidence":0.9979,"box":[[0,0],[50,0],[51,84],[0,85]]}]}}` |
| `box` | 四顶点像素坐标，顺序 左上→右上→右下→左下 |
| 错误 | 任何异常 → `HTTP 500` + `{"detail": "..."}`，无错误码体系、无部分成功 |
| 其他 | 单图单请求、无鉴权、无 batch；服务端单例串行（并发请求实际排队） |

⚠️ **返回的是「文本块」不是「整句」**：整屏实测会把一句话拆成多块。比对时必须先把所有块的 `text` **去掉全部空白再拼接**（见 `docs/ocr-data-spec.md` §10.2）。

---

## 2. 数据预处理（在开发机执行）

把 ui-annotator 的导出产物转成 PPOCR 识别（rec）训练数据。

### 2.1 一键命令

```bash
cd /home/listone/local_PP_OCRv5_server
python3 src/tools/prepare_rec_dataset.py \
  --export /mnt/d/ui-dataset/ocr_export \
  --out ./train_data/rec \
  --val-ratio 0 --write-charset
```

- 输出目录已存在时加 `--force`（只清理本工具产出的条目，不会误删你的文件）；
- `--val-ratio 0` = 不切验证集，17 条全进训练集（此时 `rec_gt_test.txt` 是**空文件**，训练时别把 `Eval.dataset.label_file_list` 指过去）；
- 要切验证集就用 `--val-ratio 0.2`，或 `--val-screens zhujiemian,jianbao` 显式指定（**同屏样本不会跨集**）。

### 2.2 自检（不需要真实数据集）

```bash
python3 src/tools/prepare_rec_dataset.py --self-test
```

### 2.3 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-e, --export` | 必填 | ui-annotator 导出根目录（含 `manifest.json` / `labels.tsv` / `crops/`） |
| `-o, --out` | `<导出目录同级>/ppocr_rec` | 输出目录 |
| `--val-ratio` | `0.2` | 验证集占比，按界面（组）挑选；`0` = 不切 |
| `--val-screens` | 空 | 显式指定验证集界面，逗号分隔，优先于 `--val-ratio` |
| `--drop-flags` | `blank` | 按质检标记剔除（词表：`blank,low_ink,edge_touch,fallback_sample,text_sanitized`） |
| `--keep-space` | 关 | 保留文本内部空格（默认去掉全部空白） |
| `--group-same-text` | 关 | 同文本多图合并成 PPOCR 的 JSON 列表行（离线增广采样） |
| `--link` | `copy` | `copy` / `hardlink` / `symlink` |
| `--write-charset` | 关 | 额外写 `charset.txt`（**仅供参考**，不能用来替换官方 dict） |
| `--force` | 关 | 输出目录非空时先清理 |
| `--dry-run` | 关 | 只统计与校验，不落盘 |
| `--allow-invalid` | 关 | 校验失败也继续（报告里 `errors` 非空，仅供调试） |

### 2.4 产物（`train_data/rec/`，已 gitignore）

```
train_data/rec/
├── rec_gt_train.txt        # 相对路径<TAB>文本（路径相对本目录）
├── rec_gt_test.txt         # 同格式；--val-ratio 0 时为空文件
├── train/                  # 平铺图片
├── test/                   # 有验证集时才有
├── charset.txt             # 训练集字符表（--write-charset）
├── rec_dataset_report.json # 来源/参数/统计/逐条映射/告警/训练命令
└── README.md               # 本次生成的说明与训练命令
```

### 2.5 注意

- 脚本会先按 `docs/ocr-data-spec.md` §9 校验导出产物（版本、统计不变式、PNG 尺寸、sha1、枚举、charset…），**不通过即中止**（退出码 1）；
- 报告里的训练命令用的是**本机绝对路径**，换到训练机后必须改 `data_dir`；
- 微调官方 `PP-OCRv5_server_rec` 时 **`character_dict_path` 必须沿用官方 `ppocrv5_dict.txt`**，不能用 `charset.txt` 替换（换 dict 会改变输出层类别数，预训练权重加载不上）。

---

## 3. 预测 / 评测（在开发机执行，需服务已拉起）

逐条 crop 送进 OCR 服务，对着 `labels.tsv`/`labels.jsonl` 算逐条准确率、耗时分布，并罗列失败数据。

### 3.1 一键命令

```bash
cd /home/listone/local_PP_OCRv5_server
python3 src/tools/eval_rec_service.py --export /mnt/d/ui-dataset/ocr_export
```

### 3.2 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-e, --export` | 二选一必填 | ui-annotator 导出根目录（推荐：带 `mode`/`screen_id`/`flags`） |
| `-l, --labels-file` | 二选一必填 | PPOCR 风格标签文件（`--data-dir` 指定图片根目录，`mode` 一律按 `contains`） |
| `-u, --url` | `http://192.168.50.2:8118/ocr` | OCR 接口 |
| `--timeout` | `60` | 单次请求超时（秒） |
| `--retries` | `0` | 单条失败后重试次数 |
| `--conf-threshold` | `0` | 丢弃置信度低于该值的识别块 |
| `--limit` | `0` | 只评测前 N 条（冒烟用） |
| `--warmup` | `1` | 评测前先发的预热请求数（不计入耗时统计） |
| `--out` | `build/eval` | 报告输出目录（已 gitignore） |
| `--fail-under` | 无 | 准确率低于该百分比时退出码 3 |
| `--verbose` | 关 | 逐条打印结果（进度条走 stderr，不冲突） |

### 3.3 报告

`build/eval/eval_report_<时间戳>.md`（人读）与同名 `.json`（机器读，含全量逐条证据）。四节内容：

1. **总览**：总条数 / 通过 / 不通过 / 请求失败 / 准确率，按 `mode`、按界面分解；
2. **耗时分布**：min / P50 / P90 / P95 / max / 均值 / 标准差 + 直方图 + 最慢 5 条；
3. **失败清单**：逐条列出「期望 / 实得 / 原因 / 耗时 / flags」，并附**原始识别块**（用于区分是切块问题还是识别错误）；
4. **逐条证据**（TagEvidence 式）：每条 crop 的期望、实得、命中与否、耗时、块数。

### 3.4 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部通过 |
| 1 | 有未通过（识别错或请求失败） |
| 2 | 用法/输入错误，或服务不可达 |
| 3 | `--fail-under` 门限未达标 |

### 3.5 实测基线（2026-09-13，可作为回归对照）

```
结果：通过 17 / 17，准确率 100.00%（不通过 0，请求失败 0）
耗时：P50 15 ms | P90 21 ms | 最大 26 ms | 合计 282 ms
```

> 这是 **crop 级**耗时（图很小，51×91 ~ 477×155）。整屏图约 300 ms，两者不要混着比。

---

## 4. 规范与文档索引

| 文档 | 内容 |
|---|---|
| [`docs/data-spec.md`](docs/data-spec.md) | 上游界面标注数据规范 v1（`sig.json` / `TextTag` / 归一化坐标） |
| [`docs/ocr-data-spec.md`](docs/ocr-data-spec.md) | **OCR 导出数据规范 v1**（产物字段、质检标记阈值、消费方 12 条硬校验） |
| [`docs/ocr-export.md`](docs/ocr-export.md) | 导出用法 / 质检标记 / 故障排查 |
| [`docs/ppocr-rec-data-spec.md`](docs/ppocr-rec-data-spec.md) | **PPOCR rec 训练数据规范**（目录结构、标签格式、`data_dir` 拼接规则、训练命令） |
| `schemas/*.json` | 机器可读 Schema（导出清单、上游签名） |
| `examples/` | 真实导出清单示例、上游 `sig.json` 示例 |
| `src/tools/prepare_rec_dataset.py` | 导出产物 → PPOCR rec 数据（预处理） |
| `src/tools/eval_rec_service.py` | 逐条 crop 识别评测（预测） |

---

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| `curl http://192.168.50.2:8118/` 连不上 | 服务没拉起。先 `ssh -F ~/.ssh/config localserver 'podman ps'`，再按 §1.1 启动 |
| 评测脚本报「服务不可达」 | 同上；脚本探活失败会直接退出（码 2），不会白跑一遍 |
| 预处理报「输出目录非空」 | 加 `--force`（只清理脚本自己产出的条目），或换空目录 |
| 预处理报校验错误并中止 | 导出产物有问题（见 `docs/ocr-data-spec.md` §9）。按报错修数据后重新导出，确要带病转换加 `--allow-invalid` |
| 输出目录里有用户文件被拒绝清理 | 你选错目录了，换一个空目录 |
| 服务器读不到 `D:\ui-dataset` | 正常——数据集在 Windows 盘上。预处理在本机跑，产物再同步到训练机 |
| 训练命令里的路径不对 | 脚本打印的是本机绝对路径，训练机要换成自己的 `data_dir` |
| 训练时 dict 该用哪个 | 官方 `ppocrv5_dict.txt`，**不要**用 `charset.txt` |

---

## 附录 A. 手动启动 / 停止服务（当前约定：无任何开机自启 / 自动拉起）

> 服务器上已不再配置开机自启（原 ocr-startup.service 已取消），
> 容器也未设置 restart 策略（进程退出不会自动重启）。
> 需要服务时，在服务器上本仓库目录手动执行：

```bash
# 启动（幂等：已在运行则直接退出；会自动检查模型盘挂载、刷新 CDI 并等待就绪）
bash start_service.sh

# 停止
podman-compose down

# 跟踪日志
podman-compose logs -f
```

## 附录 B. 容器构建与调试历史记录（原始笔记，未整理）

```bash
podman pull ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.1.0-gpu-cuda12.6-cudnn9.5

podman run -d --device nvidia.com/gpu=all --gpus all -p 8118:8118 \
   -v $(pwd)/models:/app/models \
   -v $(pwd)/test.py:/app/test.py \
   -v $(pwd)/test.jpg:/app/test.jpg \
   ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.1.0-gpu-cuda12.6-cudnn9.5 \
    sleep infinity


podman exec -it 92ec52e04a173deb3b35bf89aec9225cea5054fc774fd64d702da38fb299d6fa bash

pip install paddleocr -i https://pypi.tuna.tsinghua.edu.cn/simple

python3 -c "import paddle; print(paddle.is_compiled_with_cuda()); print(paddle.device.get_device())"


# 打包一个容器为镜像
podman commit --change='CMD ["python", "/app/src/server.py"]' 04ebe45b449e ppocr_v5_server:latest

# 再次配置
sed -i.bak 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list && \
sed -i 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list

apt-get update && apt-get install -y libglib2.0-0 libgl1 libsm6 libxext6 libxrender-dev

apt-get install -y 

# 写一个服务
podman run -d --device nvidia.com/gpu=all --gpus all -p 8118:8118 \
   -v $(pwd)/models:/app/models \
   -v $(pwd)/src:/app/src \
   ppocr_v5_server:latest \
    sleep infinity

podman exec -it 04ebe45b449e76ba956925c24251d441938208f45969132f0ecb6afa5eaadb2d bash

apt-get update && apt-get install -y libglib2.0-0 libgl1 libsm6 libxext6 libxrender-dev libgl1-mesa-glx

pip install fastapi uvicorn -i https://pypi.tuna.tsinghua.edu.cn/simple

# 运行服务
podman run -d --device nvidia.com/gpu=all --gpus all -p 8118:8118 \
   -v $(pwd)/models:/app/models \
   -v $(pwd)/src:/app/src \
   ppocr_v5_server:latest 

podman exec -it 25bae1cf08efc8e886a662dd78121ecae61b596f43edb03d0d6a99d1b0269957 bash

# 一键启动服务

podman-compose up -d

podman-compose logs -f

podman-compose down

git commit -m "这个服务对 文本分块的问题 效果特别差。一句话 分为3个文本块"
```
