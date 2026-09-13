# OCR 导出数据规范 v1

> 关联：本文档规范 **OCR 数据集导出**（把界面标注里的文本标签裁成"图片 + 文本"）产物的格式与语义。
> 上游格式见 [`data-spec.md`](data-spec.md)（界面标注数据规范 v1），使用方式见 [`ocr-export.md`](ocr-export.md)。
> 结构校验 Schema：[`schemas/ocr-export-manifest.schema.json`](../schemas/ocr-export-manifest.schema.json)；
> 示例：[`examples/ocr-export/manifest.json`](../examples/ocr-export/manifest.json)。
>
> 当前状态：**规范 v1**（对应 `export_schema_version = 1`），由 `src/ui_annotator/ocr_export.py` 产出。

---

## 1. 目标与范围

导出的用途（两条，都是 OCR 识别模型的下游）：

1. **校验**：拿识别模型逐张跑 `crops/` 里的图，与实际标签文本比对，得到逐条准确率 → 对应父仓库 `TagEvidence`；
2. **微调**：`crops/` + `labels.tsv` 即"图片—文本"训练对；`charset.txt` 供 CTC 字典参考。

本文档规定**产物长什么样、字段什么语义、消费方必须校验什么**，不规定模型结构、训练流程与目录外的组织形式。

概念模型（一条标签 → 一张裁剪图）：

```
sig.json 的 text_tag{box, expect, polarity, mode, source.sample}
        │
        │  box × 样本图宽高 → 像素矩形（+ pad，夹到图内）
        ▼
crops/<screen_id>/<tag_id>__<sample_stem>.png   ← 数据（裁剪图）
        └─ 记录：text = expect                  ← 标签（标记文本）
```

---

## 2. 硬约定（不随版本变化）

| # | 约定 | 说明 |
|---|---|---|
| 1 | **坐标只在两个地方出现** | 记录里的 `box` 是 0~1 归一化（原样搬自 `sig.json`）；`rect`/`size` 是像素。裁剪图本身不含任何坐标信息 |
| 2 | **裁剪图无损** | PNG 逐像素等于源样本对应区域（`--resize-height` 打开时会重采样，见 §7.4） |
| 3 | **文本保真** | 记录的 `text` = `expect` **原文**；只有写进 `labels.tsv` 时才把制表符/换行替换成空格（§5.1） |
| 4 | **相对路径** | 所有路径字段相对**输出根目录**，一律 `/` 分隔（Windows 上也一样），便于跨平台消费 |
| 5 | **不落盘证据** | OCR 实读文本、置信度等运行时证据不写入产物（`sig.json` 的 `source.ocr_text` 除外，它属于上游） |
| 6 | **只读上游** | 导出不改 `sig.json`、不改样本截图；产物目录可随时删除重建 |

---

## 3. 术语

| 术语 | 含义 |
|---|---|
| **pos 记录 / neg 记录** | 来自 `polarity=pos` / `polarity=neg` 的标签。pos = 该区域**应有**该文本；neg = 该区域**不应有**该文本 |
| **参照样本** | 标签在 `sig.json` 里的 `source.sample`，即人工画框时看的那张样本 |
| **origin** | 该记录来自哪张样本：`reference`（参照样本）/ `fallback`（标签没记参照样本，退回 `samples[0]`）/ `other`（`--scope all` 下的其它样本） |
| **flags** | 质检标记（§8），提示级，不影响记录可用性 |
| **裁剪图** | `crops/` 或 `neg/` 下的 PNG |

---

## 4. 目录布局

```
<输出根>/
├── crops/<screen_id>/<tag_id>__<sample_stem>.png   # pos 标签裁剪图
├── labels.tsv                                      # 消费主入口：相对路径 <TAB> 文本，无表头
├── labels.jsonl                                    # 与 labels.tsv 同序同集合，逐条全字段
├── charset.txt                                     # pos 标签出现过的非空白字符，一字符一行，按码点升序
├── neg/                                            # 仅 --include-neg 时存在
│   └── <screen_id>/<tag_id>__<sample_stem>.png
├── neg_labels.jsonl                                # 仅 --include-neg 时存在，字段同 labels.jsonl
├── manifest.json                                   # 清单：参数、统计、逐条记录、告警（机器可读，权威）
└── README.md                                       # 人读摘要（非机器接口，内容可变）
```

约定：

- `crops/` 与 `neg/` 下的文件名 = `<tag_id>__<sample_stem>.png`；`sample_stem` 为来源样本文件名去掉扩展名。
- 同一 `(screen_id, tag_id, sample)` 只产出一张图；`--scope all` 时同一标签会因样本不同而有多张。
- 空目录不产出（无 neg 时不建 `neg/`；某界面无标签时可不出目录）。
- 一个界面一个子目录，界面之间互不影响；`screen_id` 来自 `sig.json`（不取自目录名）。

---

## 5. 文件规范

### 5.1 `labels.tsv`（消费主入口）

| 项 | 规定 |
|---|---|
| 编码 | UTF-8 无 BOM |
| 换行 | `\n`（LF）；文件以 `\n` 结尾 |
| 列 | 恰好 2 列：`相对路径` `<TAB>` `文本`；**无表头** |
| 路径 | 相对输出根，`/` 分隔，必以 `crops/` 开头 |
| 文本 | `text` 的 TSV 安全形式：`\t`、`\r`、`\n` 被替换为单个空格（该替换会在记录上打 `text_sanitized`） |
| 行序 | 与 `labels.jsonl` 完全一致，等于导出顺序（`screen_id` 字典序 → 标签在 `sig.json` 中的顺序 → 样本在 `samples` 中的顺序） |
| 行数 | 恒等于 `totals.crops` |
| 内容范围 | **只含 pos 记录**；neg 记录不出现 |

示例：

```
crops/jianbao/t1__jianbao_001.png	率土简报
crops/zhujiemian/t1__zhujiemian_001.png	武将
```

### 5.2 `labels.jsonl` / `neg_labels.jsonl`

- 每行一个 JSON 对象（UTF-8，LF 结尾），字段即本记录（§6），与 `manifest.json` 的 `crops` / `neg_crops` **逐条等值**。
- `labels.jsonl` 与 `labels.tsv` 一一对应、同序；`neg_labels.jsonl` 仅在 `--include-neg` 且确有 neg 标签时存在。
- 比对时**不要**用 JSONL 的 `text` 去对 `labels.tsv` 的文本做字符串全等——TSV 侧可能已做 §5.1 的替换；要逐字比对请用 `text` 字段。

### 5.3 `charset.txt`

- 来源：`labels.tsv` 的**文本列**（已做 §5.1 替换），取全部非空白字符去重。
- 顺序：按 Unicode 码点升序，一字符一行，UTF-8，LF 结尾。
- 用途：CTC/字典配置的候选集合。**不保证**覆盖模型可能遇到的全部字符（只覆盖本次 pos 标签），也不含在标签里未出现过的字。

### 5.4 `manifest.json`

机器可读的**权威清单**。顶层字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `export_schema_version` | int | ✔ | 恒为 `1`；不兼容变更 +1（§11） |
| `tool` | str | ✔ | 产出工具与版本，如 `ui-annotator@0.1.0` |
| `generated_at` | str | ✔ | ISO 8601 带时区偏移 |
| `dataset_root` | str | ✔ | 上游数据集根目录（绝对路径，仅追溯用） |
| `output_root` | str | ✔ | 本次输出根目录 |
| `dry_run` | bool | ✔ | 正式导出恒为 `false`（`--dry-run` 不落盘任何文件） |
| `options` | object | ✔ | 本次导出参数快照，见下表 |
| `totals` | object | ✔ | 统计，见下表 |
| `files` | object | ✔ | 固定文件名：`labels`、`labels_jsonl`、`charset`、`crops` 恒为常量字符串；`neg_labels_jsonl` 与 `neg` 在**未产出 neg** 时为 `null` |
| `screens` | array | ✔ | 逐界面摘要，见下表 |
| `warnings` | str[] | ✔ | 提示级问题（不影响记录可用性） |
| `errors` | str[] | ✔ | 失败项；非空表示导出不完整（CLI 退出码 1） |
| `crops` | array | ✔ | pos 记录（§6） |
| `neg_crops` | array | ✔ | neg 记录（§6），未导出 neg 时为 `[]` |

> `files.neg_labels_jsonl` 与 `files.neg` 只在**确有 neg 产物**时是字符串，否则为 `null`；
> 判据用 `manifest.files.neg is not null` 或 `length(neg_crops) > 0`（两者等价，§9-8）。

`options`：

| 字段 | 类型 | 含义 |
|---|---|---|
| `scope` | `"source"` \| `"all"` | `source`=每条标签只裁其参照样本；`all`=该界面全部样本 |
| `pad_px` | int ≥ 0 | 四周外扩像素 |
| `pad_ratio` | number ∈ [0, 0.5) | 按框长边比例外扩（仅 `pad_px = 0` 时生效） |
| `resize_height` | int ≥ 0 | 等比缩放目标高度，`0` = 不缩放 |
| `include_neg` | bool | 是否导出 neg |
| `only_screens` | str[] | 只导出指定界面（空数组 = 全部） |

`totals`：

| 字段 | 含义 | 不变式 |
|---|---|---|
| `screens` | 成功导出的界面数 | = `length(screens)` |
| `failed_screens` | 失败项条数 | = `length(errors)` |
| `crops` | pos 裁剪图数 | = `length(crops)` = `labels.tsv` 行数 = `length(labels.jsonl)` 行数 |
| `neg_crops` | neg 裁剪图数 | = `length(neg_crops)` |
| `skipped_neg` | 未导出 neg 的标签数 | 当 `include_neg=false` 时 = 上游 neg 标签总数 |
| `warnings` | 告警条数 | = `length(warnings)` |
| `flagged` | 带 `flags` 的记录数 | = `crops` + `neg_crops` 中 `flags` 非空者个数 |

`screens[]`（逐界面摘要，仅统计不含记录）：`screen_id`、`display_name`、`transient`、`tags`（该界面标签总数，含未导出的 neg）、`samples`、`crops`、`neg_crops`。

### 5.5 `crops/` 与 `neg/` 的图片

- 格式：PNG（8 位/通道，Qt `QImage` 写出；源样本为 RGBA 时保留 alpha，否则 RGB）。
- 尺寸：等于 `size.w` × `size.h`；`resize_height = 0` 时等于 `rect.w` × `rect.h`。
- 内容：源样本中 `rect` 区域的逐像素复制（无缩放时）。
- 命名：`<tag_id>__<sample_stem>.png`；`tag_id`/`sample_stem` 中非 `[0-9A-Za-z._-]` 的字符替换为 `_`（正常数据不会触发，因为 `tag_id` 受上游命名约束）。

### 5.6 `README.md`

本次导出的中文摘要：参数、统计、告警列表、消费提示。**人读用，不是接口**；解析请一律以 `manifest.json` 为准（README 文案可随工具版本变化）。

---

## 6. 逐条记录（`crops[]` / `neg_crops[]` / `*.jsonl`）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `image` | str | ✔ | 裁剪图相对路径，`^(crops\|neg)/...png$` |
| `text` | str | ✔ | 标签文本 = `expect` 原文（§2-3） |
| `screen_id` | str | ✔ | 来源界面；满足 `^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$` |
| `tag_id` | str | ✔ | 来源标签号；`sig.json` 未写时由工具按 `t1/t2…` 生成，同界面唯一 |
| `sample` | str | ✔ | 来源样本相对 `sig.json` 的路径，如 `shots/zhujiemian_001.png` |
| `polarity` | `"pos"` \| `"neg"` | ✔ | 复述自标签 |
| `mode` | `"contains"` \| `"equals"` | ✔ | 比对语义，见 §10.2 |
| `box` | object | ✔ | 原始标注框 `{x,y,w,h}`，0~1 归一化，与 `sig.json` 完全一致 |
| `rect` | object | ✔ | 实际裁剪矩形 `{x,y,w,h}`（整数像素，含 pad，已夹到样本范围内） |
| `size` | object | ✔ | 成图尺寸 `{w,h}`（整数像素） |
| `origin` | enum | ✔ | `reference` / `fallback` / `other`（§3） |
| `sha1` | str | ✔ | 裁剪图**文件字节**的 SHA-1（40 位小写十六进制），用于查重与跨导出比对 |
| `ink_ratio` | number [0,1] | ✔ | 内容占比估计值（§8 末尾，仅供筛选参考） |
| `flags` | str[] | – | 质检标记，取值见 §8，去重 |
| `comment` | str | – | 标注时写的人工备注，原样带出 |

---

## 7. 裁剪与坐标规则（可复算）

### 7.1 归一化 → 像素

设样本图宽高为 `W × H`，标注框 `box{x,y,w,h}`（先夹到 `[0,1]`），外扩 `pad` 像素：

```
left   = round(box.x * W) - pad
top    = round(box.y * H) - pad
right  = round((box.x + box.w) * W) + pad
bottom = round((box.y + box.h) * H) + pad
left'  = clamp(left,   0, W-1)      top'    = clamp(top,   0, H-1)
right' = clamp(right,  left'+1, W)  bottom' = clamp(bottom, top'+1, H)
rect   = {x:left', y:top', w:right'-left', h:bottom'-top'}
```

- 取整用**四舍五入**（不是截断），矩形按**左闭右开**解释；`rect` 至少 1×1。
- `pad` 取值：`pad_px > 0` 时用它；否则 `pad_ratio > 0` 时用 `round(长边像素 × pad_ratio)`，长边 = `max(box.w*W, box.h*H)`；两者都为 0 时 `pad = 0`。
- 越界一律**夹到样本范围内**（不报错、不补边），因此 `rect` 可能比公式算出的小，但**永远落在样本图内**。
- 标注框完全落在图外（`rect` 为 `None`）时，该标签记入 `errors`，不产记录。

### 7.2 记录里的 `box` 是原值

`box` 保留 `sig.json` 的原值（含工具未夹取的极端值），`rect` 才是实际裁剪结果。消费方要复算请按 §7.1，不要假设 `rect == box × 图尺寸`。

### 7.3 图像取像素

`crop = 源图.copy(rect.x, rect.y, rect.w, rect.h)`，逐像素复制、无插值、无色彩管理。

### 7.4 缩放

`resize_height > 0` 时：`scaledToHeight(resize_height, Smooth)` → 高恰为 `resize_height`，宽按比例四舍五入。此时：

- `rect` 仍是**裁剪**矩形（未缩放）；
- `size` 是**缩放后**尺寸；
- 像素不再等于源图区域（§2-2 的无损保证仅在不缩放时成立）；
- `sha1` 是缩放后 PNG 的哈希。

---

## 8. 质检标记 `flags`

| 标记 | 判据（实现阈值） | 含义 / 建议 |
|---|---|---|
| `blank` | 降采样图（最长边 ≤64px）灰度极差 ≤ **12** | 整块近乎纯色（空框/纯色块）。回工具确认框位置或删标签 |
| `low_ink` | 非 blank 且内容占比 < **0.02** | 框太大或内容极少 |
| `edge_touch` | 四边都有"内容"像素 | 文字可能被框切边；可用 `--pad` 重导或回工具放大框 |
| `fallback_sample` | `origin == fallback` | 标签没记参照样本，裁剪图未必包含该文本，**需人工复核** |
| `text_sanitized` | `expect` 含 `\t`/`\r`/`\n` | TSV 侧已替换为空格；`text` 字段仍是原文 |

`low_ink` / `edge_touch` 只在"背景较纯"时判定：以边界像素的主色占比 ≥ **0.15** 为前提（实景/渐变背景下"与主色不同"不再等价于"有文字"，故只保留 `blank` 判定）。

`ink_ratio` 的定义：把裁剪图等比降采样到最长边 ≤64px，取边界像素的众数颜色为背景色，统计与该色 RGB 曼哈顿距离 > **90** 的像素占比。**在实景背景（游戏 3D 画面等）下该值会接近 1，不可作为"有文字"的判据**，只适合排序/抽查。

排序与去重：同一 `sha1` 出现在多条记录时，若 `text` 相同属正常重复（同屏多图），若 `text` 不同则导出时会产生告警——消费方应视为数据问题（同图两种标签）。

---

## 9. 硬校验规则（消费方/校验工具应执行）

结构校验按 `schemas/ocr-export-manifest.schema.json`；以下为语义校验。
其中 2–8、10 已由仓库测试**对着真实导出产物逐条执行**
（`tests/test_ocr_export.py::test_exported_manifest_matches_spec`），文档与实现一同回归：

1. `manifest.export_schema_version == 1`（不认识的版本拒绝消费）。
2. `manifest.dry_run == false`，且 `totals` 各计数与对应数组长度一致（§5.4 不变式列）。
3. `labels.tsv` 行数 == `totals.crops`；每行恰好 2 列（1 个 TAB）；路径必须落在 `crops/` 下且文件存在。
4. `labels.jsonl` 行数 == `totals.crops`，且与 `labels.tsv` **同序**、`image` 集合一致、文本互为 §5.1 关系。
5. 每条记录的 `image` 存在、是合法 PNG，且实际宽高 == `size.w` × `size.h`。
6. 文件字节的 SHA-1 == 记录的 `sha1`。
7. `rect` 在样本图范围内（`x ≥ 0`、`y ≥ 0`、`x+w ≤ W`、`y+h ≤ H`），且与 §7.1 由 `box`+`pad`+样本尺寸复算的结果一致（`resize_height > 0` 时只校验 `rect` 合法性，不校验与 `size` 相等）。
8. `neg_crops` 非空 ⟺ `files.neg == "neg/"` ⟺ 存在 `neg_labels.jsonl`；`neg_crops` 中任何 `image` **不得**出现在 `labels.tsv`。
9. `flags` 取值 ⊆ §8 词表；`origin`、`polarity`、`mode` 取值 ∈ 各自枚举。
10. `charset.txt` 的字符集合 ⊇ `labels.tsv` 文本列的非空白字符集合（允许相等；工具当前实现为相等）。
11. `screen_id`、`tag_id` 满足命名正则（§6）。
12. `sample` 能定位到上游数据集里的真实样本文件（有数据集时校验；只拿产物时跳过）。

---

## 10. 消费者契约（怎么用才不错）

1. **pos 与 neg 不可混用**：`crops/`（pos）才可作为识别模型的训练/评测样本；`neg/` 的语义是"该区域不该出现该文本"，其裁剪图**不含** `text` 内容，混入训练即错标。
2. **比对要按 `mode` + 空白归一化**：`contains` = 识别结果包含 `text`；`equals` = 归一化后全等。两种模式都必须先**去掉全部空白**再比（对抗服务端把一句话拆成多个 text 块）。这与 uiauto `TextTag` 的语义完全一致。
3. **同一 `text` 可能有多张图**（同屏多图、不同机型/主题）：这是正常增广，不要按 `text` 去重后再统计准确率；按记录逐条统计。
4. **筛选建议**：先剔除 `blank`（空框），再人工复核 `fallback_sample`；`edge_touch`/`low_ink` 只提示，不一定要剔除。
5. **可追溯**：任一记录都能沿 `screen_id`/`tag_id` 回到 `sig.json` 的具体条目（§7 映射），改标注后重新导出即可（导出幂等）。
6. **不要解析 `README.md`**：接口是 `manifest.json` + `labels.tsv`。

---

## 11. 与父仓库（uiauto）的映射

产物是标注数据的**派生物**，不承载签名语义本身；映射关系：

| 产物字段 | 上游（`sig.json` / uiauto） |
|---|---|
| `box` | `TextTag.box`（`NormBox`，0~1） |
| `text` | `TextTag.expect` |
| `polarity` / `mode` | `TextTag.polarity` / `TextTag.mode` |
| `screen_id` / `tag_id` | `ScreenSig.screen_id` / `TextTag` 的条目标识（仅审计用） |
| 校验产出（不落盘） | uiauto `TagEvidence` / `MatchReport`：期望、实读、是否通过 |

即：**导出负责"取证据材料"，判定语义仍由 uiauto 的签名规则定义**（见 `data-spec.md` §2、§7）。

---

## 12. 版本与变更

- `export_schema_version` 只在**不兼容变更**时 +1：字段删除/改名、语义改变、必填变化、目录布局或文件名改变、`labels.tsv` 列数变化。
- 以下变更**不**升版本：新增可选字段（如新的 `flags` 取值、记录新增可选键）、`README.md` 文案、告警文案、工具版本号变化。
- 消费方应：先读 `manifest.json`，版本不匹配就明确报错，而不是"尽力解析"。
- 旧版产物不做迁移；直接重新导出（导出幂等且只读上游）。

---

## 13. 示例与参考

- 示例清单：[`examples/ocr-export/manifest.json`](../examples/ocr-export/manifest.json)（取自真实数据集，含一条 `edge_touch` 记录）
- Schema：[`schemas/ocr-export-manifest.schema.json`](../schemas/ocr-export-manifest.schema.json)
- 使用与故障排查：[`ocr-export.md`](ocr-export.md)
- 上游标注规范：[`data-spec.md`](data-spec.md)

---

## 14. 待确认 / 已知限制

1. **只覆盖 pos/neg 两类裁剪**：尚无 train/val/test 划分（上游 `data-spec.md` §4.1 的 `splits/` 仍是预留）；若需要，建议由消费方按 `sha1`/`screen_id` 分组划分（同屏样本不能跨集，否则评测虚高）。
2. **neg 的用途未定**：当前只保证"单独存放、不进训练集"。若要用于负样本挖掘（如"该区域识别出该文本 = 误检"），需要另一套记录语义，届时再升规范。
3. **旋转/透视不处理**：裁剪只做轴对齐矩形，游戏 UI 截图通常正投影；倾斜文本需上游改框。
4. **`ink_ratio` 不可靠**（实景背景）——是否需要更稳的"框内是否有文字"判据，待与 OCR 结果对照后再定。
5. **不落盘 OCR 实读**：校验证据（`TagEvidence`）目前由消费方自己产生；是否让导出器可选写入一份 `evidence.jsonl`，待定。
6. **样本一致性检测未实现**：导出不做"同屏多图布局是否一致"的判定（只在 `--scope all` 时用 `origin` 标注来源）。上游数据一致性问题见 `ocr-export.md` §4。
