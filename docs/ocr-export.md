# OCR 数据导出（文本标签裁剪）

> 把数据集中**所有界面的所有文本标签**裁成 OCR 数据：**框选的图片 = 数据，标记的文本 = 标签**。
> 产物用于识别（OCR）模型的**校验**与**微调**。
> 实现：`src/ui_annotator/ocr_export.py`；测试：`tests/test_ocr_export.py`。

---

## 1. 一句话流程

```
数据集 sig.json (text_tags: box + expect)
   └─ 按 box 从 source.sample 裁出区域 → PNG           ← 数据
   └─ expect 写成该图的标签文本                        ← 标签
   └─ labels.tsv / labels.jsonl / manifest.json 落盘
```

一条 `text_tag` 产出一张裁剪图。`box` 是 0~1 归一化坐标，乘以样本实际宽高得到像素矩形，
因此同一条标签在任何分辨率的样本上都指向同一块区域（规范见 [`data-spec.md`](data-spec.md)）。

---

## 2. 两种用法

### 2.1 GUI

工具栏 **「导出 OCR 数据集」**（在「校验」右侧）：

1. 需要先打开数据集；
2. 若当前 Screen 有未保存修改，会先问你是否保存（导出读的是磁盘上的 `sig.json`）；
3. 选择输出目录，默认 `<数据集>/ocr_export`；
4. 输出目录非空时会确认一次，确认后先清空再写（仅限上一次的导出产物）；
5. 完成后弹窗给出统计报告，可点「打开输出目录」。

GUI 按钮等价于默认参数（`--scope source`、不扩边、不缩放）。需要扩边/缩放/neg/批量过滤时用命令行。

### 2.2 命令行

WSL：

```bash
cd /home/listone/game-automation/ui-annotator
.venv/bin/python -m ui_annotator.ocr_export -d /mnt/d/ui-dataset
```

Windows（运行目录 `D:\ui-annotator`）：

```powershell
D:\ui-annotator\.venv\Scripts\python.exe -m ui_annotator.ocr_export -d D:\ui-dataset
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `-d, --dataset` | 必填 | 数据集根目录（须含 `screens/`） |
| `-o, --output` | `<数据集>/ocr_export` | 输出目录 |
| `--scope source\|all` | `source` | `source`=每条标签只裁它标注时参照的样本（见 §4）；`all`=该界面所有样本都裁 |
| `--pad N` | `0` | 四周外扩像素，`0`=严格按标注框 |
| `--pad-ratio R` | `0` | 按框长边比例外扩（如 `0.05`）；`--pad` 非 0 时以 `--pad` 为准 |
| `--resize-height H` | `0` | 等比缩放到指定高度（CRNN/PP-OCR 常见 32/48），`0`=保留原始像素 |
| `--include-neg` | 关 | 一并导出 `neg` 标签到 `neg/`（不写入 `labels.tsv`） |
| `--only-screen ID` | 全部 | 只导出指定界面，可重复 |
| `--force` | 关 | 输出目录非空时先清空（仅当该目录含 `manifest.json`，即上一次的导出产物） |
| `--dry-run` | 关 | 只统计与检查，不写任何文件 |

退出码：`0` 成功；`1` 有界面/样本失败（其余界面照常导出）；`2` 参数或数据集不可用。

---

## 3. 产物布局

```
<输出>/
├── crops/<screen_id>/<tag_id>__<sample_stem>.png   # pos 标签裁剪图（数据）
├── labels.tsv                                      # 相对路径 <TAB> 标记文本（标签）
├── labels.jsonl                                    # 同上，逐条全字段
├── charset.txt                                     # pos 标签出现过的字符表（一字符一行）
├── neg/<screen_id>/...                             # neg 标签裁剪图（仅 --include-neg）
├── neg_labels.jsonl
├── manifest.json                                   # 参数 / 统计 / 逐条记录 / 告警
└── README.md                                       # 本次导出的说明与告警摘要
```

- `labels.tsv`：每行 `crops/alpha/t1__alpha_001.png<TAB>确定`，路径相对本目录，**无表头**，
  可直接作为识别模型的标签文件。
- `labels.jsonl` / `manifest.json` 的逐条记录字段：

  | 字段 | 含义 |
  |---|---|
  | `image` | 裁剪图相对路径 |
  | `text` | 标签文本（`expect` 原文） |
  | `screen_id` / `tag_id` / `sample` | 来源界面 / 标签号 / 来源样本 |
  | `polarity` / `mode` | `pos`\|`neg`、`contains`\|`equals`（比对语义） |
  | `box` | 标注框（归一化）；`rect` / `size` | 实际裁剪像素矩形 / 成图尺寸 |
  | `origin` | `reference`=标注时参照的样本，`fallback`=标签没记参照样本而退回 `samples[0]`，`other`=`--scope all` 下的其它样本 |
  | `sha1` | 裁剪图内容哈希（查重、跨屏比对用） |
  | `ink_ratio` | 以边界主色为背景估计的内容占比（实景背景会接近 1，仅供参考） |
  | `flags` | 质检标记，见 §5 |
  | `comment` | 标注时写的人工备注 |

- `neg` 标签：语义是"该区域**不该**出现 `expect` 这段文本"，把它的裁剪图当成识别样本会得到错标签，
  所以默认不导出，导出时也单独放 `neg/`、单独一份 `neg_labels.jsonl`，**不要混进识别模型的训练集**。

---

## 4. 为什么默认只裁 `source.sample`（重要）

规范允许一个界面有多张样本（不同分辨率/机型/主题）。但**同屏多图必须是同一套布局**，
否则归一化框会落到别的内容上。实测当前数据集就踩了这个坑：

| 界面 | 现象 |
|---|---|
| `zhujiemian` | `_002` 里 5 个标签框区域几乎**全空**（框内无内容），pos 标签在它上面不成立 |
| `tishi_xiazaiziyuan` | `_002` 的「确定/取消」按钮位置与框不符（框内平均差 257/245，满量程 765） |

因此默认 `--scope source`：每条标签只从**它标注时参照的那张样本**（`source.sample`，画框时自动记录）
上裁一张，保证"图片 ↔ 标签"必然对应。`--scope all` 会把这些错位样本一起裁进来，
记录里以 `origin=other` 标出——要用就得先修样本（删掉不同布局的截图，或为它另开 `screen_id`）。

> 这也说明这类样本本身过不了规范 §6-7 的"自家样本必须全命中"交叉校验：修数据比调模型收益大。

---

## 5. 质检标记（`flags`）

| 标记 | 含义 | 建议 |
|---|---|---|
| `blank` | 裁剪图灰度极差 ≤ 12，近乎纯色（空框/纯色块） | 回标注工具确认框位置，或删掉该标签 |
| `low_ink` | 内容占比 < 2%（框太大 / 内容极少） | 收窄框，或确认该区域确实有文字 |
| `edge_touch` | 四边都有内容，字可能被框切掉 | 用 `--pad 2` 重新导出，或回工具把框放大 |
| `fallback_sample` | 标签没记 `source.sample`，退回了该界面第一张样本 | 复核这张图的文本是否真的匹配 |
| `text_sanitized` | 标签含制表符/换行，写 TSV 时替换成了空格（`jsonl` 里保留原文） | 一般无需处理 |

`blank` / `low_ink` 出现时命令输出与 `manifest.json` 的 `warnings` 里会逐条列出。
`edge_touch` / `low_ink` 只在"背景较纯"（边界主色占比 ≥ 15%）时判定，避免实景渐变背景误报。

---

## 6. 拿到数据之后

**校验现有识别模型**：

```python
# labels.tsv 每行 = 一张裁剪图 + 应有文本；逐条跑模型比对即可得到准确率
for line in open("ocr_export/labels.tsv", encoding="utf-8"):
    path, expect = line.rstrip("\n").split("\t")
    actual = my_ocr(Image.open(path))            # 你的识别模型
    ok = expect in actual if mode == "contains" else expect == actual
```

- `manifest.json` 的 `mode` 区分 `contains`（子串命中）与 `equals`（归一化后全等），
  与 uiauto `TextTag` 的语义一致，比对时请按同一规则；
- 规范要求比对前做**空白归一化**（去掉全部空白），否则服务端把一句话拆成多个 text 块会误判；
- 逐条证据（哪张图、期望什么、识别成什么）就是 uiauto `TagEvidence` 的输入。

**微调识别模型**：`crops/` + `labels.tsv` 即"图片—文本对"。
需要统一高度时用 `--resize-height 32`（或 48）重新导出；
`charset.txt` 可直接当 CTC 字典候选（按需增删空白/未知符）。

**回写标注**：如果发现某条标签文本写错了（如框选对了但 `expect` 打错字），
回 GUI 改 `expect` 后重新导出即可——导出是幂等的，产物可随时删掉重生成。

---

## 7. 出问题了怎么看（故障排查）

失败弹窗的标题是「**导出 OCR 数据集失败**」，正文是具体原因；点「显示详细信息」能看到完整堆栈。
同时**完整堆栈会写进 GUI 日志**：

```
C:\Users\<你>\AppData\Roaming\LiStone30\ui-annotator\lan\ui-annotator.log
```

（WSL 侧对应 `~/.config/... ` 视平台而定；GUI 启动时日志路径会打印在日志首行。）

常见原因与处理：

| 弹窗里的原因 | 怎么处理 |
|---|---|
| `输出目录非空：…（加 --force 覆盖，或换一个空目录）` | 目录里有别的东西且没允许清空。GUI 里确认"是否继续"即可；CLI 加 `--force` |
| `…里有不是本工具产出的文件（如 xxx），已拒绝清空以免误删` | 你选错了目录（选到桌面/项目目录等）。换一个空目录 |
| `无法清空 …：文件被其它程序占用` | **Windows 最常见**：资源管理器预览窗格、看图软件、或训练脚本正打开着导出目录里的 PNG。关掉它们后重试（点过成功弹窗里的「打开输出目录」时尤其容易撞上） |
| `无法创建输出目录 …：…` | 路径不存在/无权限/带非法字符。注意从资源管理器"复制为路径"会带引号，本工具会自动剥掉，但手打时别留怪字符 |
| `输出目录不能是数据集根目录或 screens 目录` | 换个输出目录 |
| `数据集缺少 screens 目录：…` | `-d` 指错了目录 |
| `报告里出现「失败 N 项」`（退出码 1） | 某个界面 `sig.json` 坏了或样本 PNG 读不出来；其余界面照常导出，按报告逐条修 |

**导出中途失败会留半成品**（有 `crops/` 但没有 `manifest.json`）。这种情况下再次导出并允许清空即可覆盖重来——
工具判断"这是不是自己的产物"看的是目录内容，不依赖 `manifest.json`。

---

## 8. 边界与限制

- 只做**裁剪**，不做 OCR 识别、不做二值化/去噪/倾斜校正；旋转/透视变形不处理（游戏 UI 截图通常是正投影）。
- `--scope source` 时数据量 = 标签数（当前数据集 17 条 → 17 张），样本多但都是可靠的；
  想要更多数据应当先补标（同一布局的多张真机截图），而不是放宽 scope。
- `--force` 清空前会检查目录内容：只清理由本工具产出的条目（`crops/`、`labels.tsv`、`manifest.json` 等），
  非导出目录（例如误选到桌面或项目目录）会被拒绝，防止误删用户文件。
- 导出只读数据集，不改 `sig.json`、不动样本截图。
- 导出失败时输出目录可能保持原样，也可能留下半成品；重新导出并允许清空即可覆盖。

---

## 9. 相关

- **产物数据规范（字段/阈值/校验规则）**：[`ocr-data-spec.md`](ocr-data-spec.md)
- 数据格式规范：[`data-spec.md`](data-spec.md)
- 数据结构校验 Schema：[`schemas/screen-sig.schema.json`](../schemas/screen-sig.schema.json)、
  [`schemas/ocr-export-manifest.schema.json`](../schemas/ocr-export-manifest.schema.json)
- 示例清单：[`examples/ocr-export/manifest.json`](../examples/ocr-export/manifest.json)
- 父仓库（uiauto）的 `ScreenSig` / `TextTag` / `TagEvidence` 映射：`data-spec.md` §7
