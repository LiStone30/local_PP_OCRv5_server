# 界面标注数据规范 v1

> 关联项目：game-automation / uiauto（父仓库）。本规范定义"界面标注辅助工具"产生与消费的
> 数据文件格式与语义，使其输出能直接映射为 uiauto 的 `ScreenSig` / `TextTag` / `Signature`
> （见父仓库 `uiauto/model.py`、`uiauto/signature.py`、`README.md §3/§5/§6`）。
>
> 当前状态：**草案 v1**。父仓库 Step 1（区域 OCR 文本判定界面）为当前唯一启用能力，
> YOLO/模板比对（锚点框）仅预留结构。末尾「待确认」列出开放问题。

---

## 1. 目标与范围

标注工具要解决的事（对应父仓库 §5 剩余工作第 2 条"界面标签的手动配置工作流"）：

- 为每个**界面(screen)** 收集**样本截图(shots)**；
- 人工在样本上**标注区域文本标签**（正/反 TextTag），形成该界面的**签名(signature)**；
- 离线**校验**签名：自家样本必须全命中、别家样本必须全不命中，并给出逐标签证据。

本规范只规定**数据文件长什么样、语义是什么**；工具界面形态、校验算法实现不在此列。

标注内容分三块，按阶段启用：

| 块 | 内容 | 阶段 | 对应 uiauto 结构 |
|---|---|---|---|
| A. 文本标签 | 区域框 + 期望文本 + 极性 + 匹配模式 | **现在启用(Step 1)** | `Signature.text` / `TextTag` |
| B. 锚点框 | 组件/模板锚点 + 存在性 + 区域 | 预留(YOLO/模板) | `Signature.need` / `forbid` / `AnchorCue` |
| C. 样本与划分 | 界面样本清单、train/val/test 划分 | 阶段 2 | `ScreenRegistry.evaluate_all` 的输入 |

---

## 2. 继承自父仓库的硬约定（不许违背）

坐标、命名、匹配语义**直接沿用** uiauto，标注数据必须与之同构：

1. **坐标系**：原点 = 画面左上角，x 向右，y 向下（标准图像坐标）。
2. **一律存 0~1 归一化坐标**（`NormBox`：`x,y,w,h` 全为相对画面宽高的比例），
   与分辨率/机型无关；像素坐标只在"裁图/采集"瞬间出现，**永不落盘**。
3. **命名**：`screen_id` / `anchor_id` / `tag_id` 一律小写下划线，如 `main_menu`、`btn_login`；
   同一数据集内 `screen_id` 全局唯一，一个锚点名全局唯一。
4. **标签语义**（见父仓库 `signature.py` docstring 与 §3 硬规则）：
   - `pos` 通过 = 该区域内 OCR 实际文本**匹配** `expect`；
   - `neg` 通过 = 该区域内 OCR 实际文本**不匹配** `expect`（不该出现的没出现）；
   - 界面命中 = 全部 `pos` 标签通过 **且** 全部 `neg` 标签通过；
   - 比对前对双方做**空白归一化**（去掉全部空白），对抗服务端把一句话拆成多个 text 块；
   - `contains`（默认）：`expect` 是实际文本的子串（适合标题/按钮文案）；
   - `equals`：空白归一化后 `actual == expect`（适合整行唯一文本）。
5. **neg 优先放**：遮罩、退出按钮、"盖着弹窗的假主界面"等不该出现却常出现的内容。

---

## 3. 概念模型

```
数据集 dataset_root
 └─ 界面 screen(screen_id)           如 main_menu
     ├─ sig.json                     该界面签名标注(本规范核心文件)
     └─ 样本 shots: *.png            真值截图, ≥1 张, 同屏可多张(不同分辨率/机型/主题)
```

- **screen_id**：一个"布局一致"的界面。布局不同（元素相对位置/文本集合有结构性差异）
  必须另开 `screen_id`（或变体后缀，如 `main_menu_vip`），**不许**混在同一个签名下。
  分辨率/宽高比不同不算布局差异（归一化坐标天然兼容）。
- **样本**：真实游戏画面截图（PNG，与 `adb screencap -p` 输出一致），是校验与训练的
  真值来源。一张样本只属于一个界面。
- **签名**：该界面"同时满足哪些视觉证据"的描述 = 一组 `text_tags`（+ 预留 `anchors`）。
- **证据(evidence)**：运行时每条标签的判定产物（期望/极性/模式/是否通过/实际 OCR 文本），
  对应 uiauto `TagEvidence` / `MatchReport`，供校验工具与训练数据采集使用，**不落盘**（临时产物）。

---

## 4. 目录布局

### 4.1 数据集目录（截图与标注产物，体积大，不入 git）

由工具/脚本通过参数指定（如 `-d <dataset_root>`），本仓库不随 git 携带真实截图。

```
<dataset_root>/
├── README.md                     # 可选：采集说明(游戏/账号/日期/机型)
└── screens/
    ├── main_menu/
    │   ├── sig.json              # 界面签名标注(见 §5)
    │   └── shots/
    │       ├── main_menu_001.png
    │       └── main_menu_002.png
    └── login/
        ├── sig.json
        └── shots/
            └── login_001.png
```

（阶段 2 预留：`<dataset_root>/splits/{train,val,test}.json`，内容为样本相对路径清单；
划分只改清单、不改 `sig.json` 与截图。）

### 4.2 仓库内目录（规范 + 工具 + 示例）

```
ui-annotator/
├── README.md
├── docs/data-spec.md             # 本文档
├── schemas/screen-sig.schema.json
└── examples/screens/<screen_id>/sig.json   # 格式示例(不含真实截图)
```

---

## 5. sig.json 文件规范（每界面一份）

### 5.1 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | int | ✔ | 当前恒为 `1` |
| `screen_id` | str | ✔ | 见 §2 命名；同数据集全局唯一 |
| `display_name` | str | – | 人类可读名（如 "主界面"），仅展示 |
| `transient` | bool | – | 过路界面（加载/结算转场），默认 `false`；语义同 `ScreenSig.transient` |
| `description` | str | – | 界面说明/采集备注 |
| `samples` | str[] | ✔ | 样本相对路径（相对本 `sig.json` 所在目录），见 §5.3 |
| `text_tags` | TextTag[] | ✔ | 文本标签列表，见 §5.2；**至少 1 条且至少 1 条 `pos`** |
| `anchors` | AnchorEntry[] | – | 锚点框列表（阶段 B 预留），默认 `[]` |
| `meta` | object | – | 编辑元数据（谁/何时/工具版本），见 §5.4 |

### 5.2 TextTag 条目

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `tag_id` | str | – | 生成 | 便于审计/去重，如 `t1`；规则同 screen_id 命名 |
| `box` | NormBox | ✔ | – | `{x,y,w,h}` 归一化矩形，约束见 §6 规则 2 |
| `expect` | str | ✔ | – | 期望文本（`neg` 时 = 不该出现的文本）；空白归一化后非空 |
| `polarity` | `"pos"` \| `"neg"` | ✔ | – | **显式必填**（数据文件要求可读性，不沿用代码层默认值） |
| `mode` | `"contains"` \| `"equals"` | – | `"contains"` | 匹配模式，语义见 §2-4 |
| `source` | object | – | – | 标注依据（工具自动填）：`sample`(参照样本相对路径)、`ocr_text`(该区域 OCR 实读)、`confidence`(0~1) |
| `comment` | str | – | – | 人工备注 |

**工具默认流**：在样本上画框 → 自动对该区域 OCR → `expect` 预填实读文本、`polarity=pos`、
`mode=contains` → 人工可改极性/模式/文本。即"先圈再确认"，不是手敲坐标。

### 5.3 samples 规则

- 值 = 相对本 `sig.json` 所在目录的路径（如 `shots/main_menu_001.png`），一律 `/` 分隔。
- **至少 1 张**，且该路径必须存在、是合法 PNG。
- 约定命名：`<screen_id>_<3 位序号>.png`（序号从 001 起），工具采集时自动编号。

### 5.4 AnchorEntry（阶段 B 预留，本期不校验命中）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `anchor` | str | ✔ | 锚点名（YOLO 类名/模板注册名），全局唯一 |
| `present` | bool | – | `true`=应存在(need)/`false`=应缺席(forbid)，默认 `true` |
| `box` | NormBox | – | 区域约束；不填 = 全屏 |
| `comment` | str | – | 备注 |

### 5.5 meta（工具写，人工不必维护）

```json
"meta": {
  "created_by": "ui-annotator@0.x",
  "created_at": "2025-09-07T12:00:00+08:00",
  "updated_at": "2025-09-07T12:00:00+08:00",
  "screen_version": 1
}
```

---

## 6. 硬校验规则（校验工具必须逐条执行，违反即报错并给出条目定位）

结构校验按 `schemas/screen-sig.schema.json`；下列为语义校验：

1. **命名**：`screen_id`/`anchor`/`tag_id` 匹配 `^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$`；
   `screen_id` 在同数据集内全局唯一。
2. **NormBox 边界**（与 uiauto `NormBox.__post_init__` 完全一致）：
   `0 <= x < 1`、`0 <= y < 1`、`0 < w <= 1`、`0 < h <= 1`、`x+w <= 1+1e-9`、`y+h <= 1+1e-9`。
3. **expect**：空白归一化后非空（`"".join(expect.split()) != ""`）。
4. **数量下限**：`text_tags` ≥ 1 且其中 `polarity=pos` 的 ≥ 1（零 pos 的签名会"空真"命中一切）。
5. **重复**：同屏内 `(box, expect, polarity, mode)` 全等的条目去重报错；同 `box` 多条的
   `expect` 冲突（归一化后不等）给出告警——同一区域两条不同期望多半是样本选错或布局混入。
6. **样本**：`samples` ≥ 1、路径存在、文件为合法 PNG；不同 `screen_id` 不得引用同一张截图。
7. **交叉判定（校验工具的核心，按父仓库 §3 硬规则 1）**：对每个**非 transient** 界面 A，
   用 A 的签名对**全部样本**（含别的界面的样本）逐一判定：
   - A 自家样本 → 必须 `ok`（有缺失就按证据提示补 pos 标签）；
   - 别家样本 → 必须 **不 ok**（误命中就按证据提示补 neg 标签，优先遮罩/干扰文本）；
   - 报告逐条给 `MatchReport`（ok/missing/intruders/evidence，对齐 uiauto `MatchReport`）。
   - transient 界面不参与"作为判定对象"的交叉校验，但其**样本仍作为其它界面的负样本**参与。
8. **导出等价性**：`sig.json` 丢弃 `source/meta/comment/tag_id` 后可无损转成 uiauto
   `ScreenSig(screen_id, transient, text=[TextTag(box, expect, polarity, mode), ...])`
   （anchors 暂映射为 `need/forbid` 的空占位，阶段 B 启用后再映射）。

---

## 7. 与 uiauto 的映射（一段即通）

```python
# (示意；导出器实现后放工具代码里)
from uiauto.model import NormBox, ScreenId
from uiauto.signature import ScreenSig, TextTag

sig: dict  # 一份 sig.json
screens.append(ScreenSig(
    screen_id=sig["screen_id"],
    transient=sig.get("transient", False),
    text=tuple(
        TextTag(
            NormBox(**t["box"]), t["expect"], t["polarity"], t.get("mode", "contains")
        )
        for t in sig["text_tags"]
    ),
))
# 之后 registry.add(...) → registry.evaluate_all(vision, 全部样本) 即完成 §6-7 交叉校验。
```

---

## 8. 示例

- 完整示例文件：`examples/screens/main_menu/sig.json`
- Schema：`schemas/screen-sig.schema.json`
- 由本规范派生的 OCR 数据导出（文本标签裁剪成「图片 + 文本」供识别模型校验/微调）：
  用法见 [`ocr-export.md`](ocr-export.md)，产物格式规范见 [`ocr-data-spec.md`](ocr-data-spec.md)

---

## 9. 变更与版本

- `schema_version` 只在**不兼容变更**时 +1（字段删除/语义改变/必填变化）；
  新增可选字段 + 工具升级可保持 `1`。
- 每个 `screen_id` 一个独立文件，天然可并行标注、可 diff、可回滚。

---

## 10. 工具实现决策与后续问题

当前 MVP 已确认：真实数据集使用工具选择的外部目录；截图通过 `adb exec-out screencap -p`
采集，同时允许导入已有 PNG；文件名固定为 `sig.json`；支持 `contains` 和 `equals`，默认
`contains`；暂不增加多人审核字段。

后续阶段仍需确认：

1. 同一屏存在"文本会变"的区域（如次数、账号名）如何处理：`contains` 取稳定前缀 /
   该区不标 / 每变体一条标签？要不要支持"该区忽略"标记？
2. 变体界面（布局小差异）命名与归属：独立 `screen_id` 还是 `main_menu_v2` 约定？
3. 阶段 B 锚点框的类别表（AnchorId 词典）与 yolo 训练数据导出格式（YOLO txt）何时定？
4. 后续是否需要多人协作/审核状态字段（draft/review/approved）？
