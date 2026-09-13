# PPOCR 识别（rec）训练 / 测试数据规范

> 本仓库做的是**文本识别**：输入已经是 ui-annotator 裁好的小图，**不做检测定位**。
> 上游数据规范（标注与导出）见 [`data-spec.md`](data-spec.md)、[`ocr-data-spec.md`](ocr-data-spec.md)。
> 本文档记录 **PPOCR 侧要求的目录结构、标签文件与配置字段**，是 `src/tools/prepare_rec_dataset.py`
> 转换脚本的输出契约。

---

## 1. 任务前提

| 项 | 取值 |
|---|---|
| 任务 | 识别（rec）。**不用 det**：图片已按标注框裁好，不训练检测定位 |
| 训练输入 | `crops/` 里的小图 + 对应文本 |
| 数据来源 | ui-annotator 导出的 `crops/` + `labels.tsv`（见 `ocr-data-spec.md`） |

---

## 2. 目录结构

PPOCR 默认把训练数据放在 `PaddleOCR/train_data/` 下：

```
PaddleOCR/train_data/
└── rec/                          # 识别任务（检测才用 det/）
    ├── rec_gt_train.txt          # 训练集标签文件
    ├── rec_gt_test.txt           # 验证集标签文件（格式同训练集）
    ├── train/                    # 训练集图片文件夹
    │   ├── word_001.png
    │   └── ...
    └── test/                     # 验证集图片文件夹
        └── ...
```

硬约定：

1. 图片**全部平铺**在 `train/` 与 `test/` 里，**不要**多层子目录散落；
2. 标签 `.txt` 与 `train/`、`test/` **同级**，不要放进图片文件夹内；
3. 磁盘上已有数据集时可用软链接挂到 `train_data/` 下，避免复制。

---

## 3. 标签文件规范（rec）

纯文本 `.txt`，**每行一张图**，图片路径与文本之间**必须用 `\t`（制表符）分隔**（用空格会训练报错）：

```
train_data/rec/train/word_001.jpg	简单可靠
train_data/rec/train/word_002.jpg	让复杂的世界更简单
```

- 路径可以是**绝对路径**，也可以是**相对 PaddleOCR 根目录的相对路径**（推荐，便于迁移）；
- 标签 = 该图的**完整文本内容**，不带额外字段；
- 离线数据增强采样：把**标签相同**的多张图路径写成一行 JSON 列表，训练时随机取一张：

```
["11.jpg", "12.jpg"]	简单可靠
["21.jpg", "22.jpg", "23.jpg"]	让复杂的世界更简单
```

> 检测（det）标签格式不同（`路径 \t [{"transcription":..., "points":[[x,y]x4]}]`，四点顺时针，无文字用 `###`），
> 本仓库**不使用**，仅备查。

---

## 4. 配置文件规范（Train / Eval 数据集字段）

PPOCR 用 YAML（如 `configs/rec/PP-OCRv3/ch_PP-OCRv3_rec.yml`）里的 `Train.dataset` / `Eval.dataset` 组合路径：

| 字段 | 含义 | 示例 |
|---|---|---|
| `name` | 数据集类名 | `SimpleDataSet` |
| `data_dir` | 图片存放**根目录** | `./train_data/rec` |
| `label_file_list` | 标签文件路径列表 | `["./train_data/rec/rec_gt_train.txt"]` |

```yaml
Train:
  dataset:
    name: SimpleDataSet
    data_dir: ./train_data/rec
    label_file_list:
      - ./train_data/rec/rec_gt_train.txt
    transforms:
      - DecodeImage: {img_mode: BGR, channel_first: false}

Eval:
  dataset:
    name: SimpleDataSet
    data_dir: ./train_data/rec
    label_file_list:
      - ./train_data/rec/rec_gt_test.txt
```

**路径解析规则（关键）**：

> 最终图片路径 = `data_dir` + 标签文件里的图片路径

所以 `data_dir=./train_data/rec` + 标签行 `train/word_001.jpg` → 实际读 `./train_data/rec/train/word_001.jpg`。
本仓库转换脚本产出的标签文件**写的正是 `train/xxx.png` / `test/xxx.png` 这种相对形式**。

**命令行覆盖**（微调时不必改 YAML）：

```bash
python tools/train.py -c <你的 rec 配置文件> -o \
  Train.dataset.data_dir=./train_data/rec \
  Train.dataset.label_file_list=["./train_data/rec/rec_gt_train.txt"] \
  Eval.dataset.data_dir=./train_data/rec \
  Eval.dataset.label_file_list=["./train_data/rec/rec_gt_test.txt"]
```

---

## 5. 与上游（ui-annotator 导出）的映射

| PPOCR 侧 | 来自导出产物的哪个字段 | 规则 |
|---|---|---|
| `train/`、`test/` 里的图 | `crops/<screen_id>/<tag_id>__<sample_stem>.png` | 平铺改名 `<screen_id>__<tag_id>__<sample_stem>.png`，只取 **pos** 记录 |
| 标签文本 | 记录 `text`（= 标注 `expect` 原文） | 规范化：去掉全部空白（与 `ocr-data-spec.md` §10.2 的比对规则一致），制表符/换行不可能进标签 |
| 划分 | `screen_id`（+ `sha1` 归并） | **同屏样本不得跨集**（`ocr-data-spec.md` §14.1），同一 `sha1` 归并到同一侧 |
| `neg/` | `neg_crops` / `neg_labels.jsonl` | **一律丢弃**，不得进训练/评测集（规范 §10.1） |
| 质检 `flags` | 记录 `flags` | `blank` 默认剔除；`fallback_sample` 记入报告待人工复核；`edge_touch`/`low_ink` 只提示 |
| 字典 | `charset.txt` | **仅供参考**：只有本次 pos 标签出现过的字符。微调官方模型必须沿用**官方 dict**，不可用 `charset.txt` 替换 |

> ⚠️ 微调 `PP-OCRv5_server_rec` 时，`character_dict_path` 必须指向官方 `ppocrv5_dict.txt`：
> 换 dict 会改变输出层类别数，官方预训练权重无法直接加载（除非改配置并接受重初始化）。

---

## 6. 相关

- 转换脚本：`src/tools/prepare_rec_dataset.py`（导出产物 → 本文档要求的目录与标签）
- 上游产物规范：[`ocr-data-spec.md`](ocr-data-spec.md)、用法 [`ocr-export.md`](ocr-export.md)
- 上游标注规范：[`data-spec.md`](data-spec.md)
