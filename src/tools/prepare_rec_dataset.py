#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ui-annotator 的 OCR 导出产物规范化为 PPOCR 识别（rec）训练 / 测试数据。

输入：ui-annotator 导出根目录（含 crops/ + labels.tsv + labels.jsonl + manifest.json）
      格式见 docs/ocr-data-spec.md
输出：PPOCR rec 数据目录（train/ + test/ + rec_gt_train.txt + rec_gt_test.txt）
      格式见 docs/ppocr-rec-data-spec.md

只做识别（rec）：图片已按标注框裁好，不做检测定位。

用法（示例）：
    python3 src/tools/prepare_rec_dataset.py \
        --export /mnt/d/ui-dataset/ocr_export \
        --out /mnt/d/ui-dataset/ppocr/rec \
        --val-ratio 0.2

自检（不需要真实数据集）：
    python3 src/tools/prepare_rec_dataset.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SCHEMA_VERSION = 1
FLAG_VOCAB = {"blank", "low_ink", "edge_touch", "fallback_sample", "text_sanitized"}
POLARITY_VOCAB = {"pos", "neg"}
MODE_VOCAB = {"contains", "equals"}
ORIGIN_VOCAB = {"reference", "fallback", "other"}
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
IMAGE_RE = re.compile(r"^(crops|neg)/.+\.png$")
UNSAFE_RE = re.compile(r"[^0-9A-Za-z._-]")
PNG_SIG = b"\x89PNG\r\n\x1a\n"

# 本工具在输出根下会自己写出的条目；--force 只允许清理这些
OWN_ARTIFACTS = (
    "train",
    "test",
    "rec_gt_train.txt",
    "rec_gt_test.txt",
    "rec_dataset_report.json",
    "charset.txt",
    "README.md",
)

DEFAULT_PPOCR_CONFIG = "configs/rec/PP-OCRv5/PP-OCRv5_server_rec.yml"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def png_size(path: Path):
    """只读 PNG 头部取宽高（不依赖 PIL）。返回 (w, h) 或 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(33)
    except OSError:
        return None
    if len(head) < 33 or head[:8] != PNG_SIG or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_lines(path: Path):
    text = path.read_text(encoding="utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n") if text else []


def sanitize_name(name: str) -> str:
    return UNSAFE_RE.sub("_", name)


def normalize_text(text: str, keep_space: bool) -> str:
    """规范化标签文本。

    默认去掉**全部空白**（与 ocr-data-spec.md §10.2 的比对规则一致）；
    keep_space=True 时保留内部空格，只把 \\t/\\r/\\n 换成空格并去掉首尾空白。
    """
    if keep_space:
        return re.sub(r"[\t\r\n]+", " ", text).strip()
    return "".join(text.split())


# --------------------------------------------------------------------------
# 读入 ui-annotator 导出产物
# --------------------------------------------------------------------------
def load_export(root: Path, warn):
    """返回 dict(manifest, records, tsv_rows, charset, files)"""
    mpath = root / "manifest.json"
    if not mpath.is_file():
        raise SystemExit(
            "[错误] 找不到 %s —— --export 需指向 ui-annotator 的导出根目录"
            "（含 manifest.json / labels.tsv / crops/）" % mpath
        )
    manifest = read_json(mpath)

    records = list(manifest.get("crops") or [])
    jl = root / "labels.jsonl"
    if jl.is_file():
        rows = [json.loads(line) for line in read_lines(jl) if line.strip()]
        if len(rows) == len(records):
            records = rows  # 字段更全（含 flags/sha1/mode），与 manifest 逐条等值
        else:
            warn(
                "labels.jsonl 行数(%d) != manifest.crops(%d)，改用 manifest 作为记录来源"
                % (len(rows), len(records))
            )

    tsv_rows = []
    ts = root / "labels.tsv"
    if ts.is_file():
        tsv_rows = [line.split("\t") for line in read_lines(ts) if line != ""]

    charset = set()
    cs = root / "charset.txt"
    if cs.is_file():
        charset = {ch for ch in read_lines(cs) if ch}

    return {
        "manifest": manifest,
        "records": records,
        "tsv_rows": tsv_rows,
        "charset": charset,
        "files": {
            "manifest": "manifest.json" if mpath.is_file() else None,
            "labels_tsv": "labels.tsv" if ts.is_file() else None,
            "labels_jsonl": "labels.jsonl" if jl.is_file() else None,
            "charset": "charset.txt" if cs.is_file() else None,
        },
    }


# --------------------------------------------------------------------------
# 校验（ocr-data-spec.md §9 的消费前检查）
# --------------------------------------------------------------------------
def verify_export(export_root: Path, data: dict, errors, warn):
    manifest = data["manifest"]
    records = data["records"]
    tsv_rows = data["tsv_rows"]
    charset = data["charset"]

    # §9-1 / §9-2
    if manifest.get("export_schema_version") != SCHEMA_VERSION:
        errors.append(
            "export_schema_version=%r，本工具只支持 %d（规范 §12：版本不匹配应明确报错）"
            % (manifest.get("export_schema_version"), SCHEMA_VERSION)
        )
    if manifest.get("dry_run") is not False:
        errors.append("manifest.dry_run=%r，dry-run 产物不含文件（规范 §9-2）" % manifest.get("dry_run"))

    totals = manifest.get("totals") or {}
    crops = manifest.get("crops") or []
    neg = manifest.get("neg_crops") or []
    screens = manifest.get("screens") or []
    warnings = manifest.get("warnings") or []
    for key, actual in (
        ("screens", len(screens)),
        ("crops", len(crops)),
        ("neg_crops", len(neg)),
        ("warnings", len(warnings)),
    ):
        if totals.get(key) != actual:
            errors.append("totals.%s=%r 与实际 %d 不一致（规范 §5.4 不变式）" % (key, totals.get(key), actual))
    flagged = sum(1 for r in crops + neg if r.get("flags"))
    if totals.get("flagged") != flagged:
        errors.append("totals.flagged=%r 与实际 %d 不一致" % (totals.get("flagged"), flagged))

    # §9-3 / §9-4
    if tsv_rows:
        if len(tsv_rows) != totals.get("crops"):
            errors.append("labels.tsv 行数 %d != totals.crops %r" % (len(tsv_rows), totals.get("crops")))
        if any(len(row) != 2 for row in tsv_rows):
            errors.append("labels.tsv 存在列数 != 2 的行（必须恰好 1 个 TAB）")
        if [row[0] for row in tsv_rows] != [r.get("image") for r in records]:
            errors.append("labels.tsv 与记录集合/顺序不一致（规范 §9-4 要求同序同集合）")
    else:
        warn("导出目录没有 labels.tsv，只按 labels.jsonl/manifest 转换")

    # §9-5/6/7/9/11 逐条
    for rec in records:
        tag = rec.get("image") or "<无 image>"
        image = rec.get("image") or ""
        if not IMAGE_RE.match(image):
            errors.append("%s: image 不符合 ^(crops|neg)/...png$" % tag)
            continue
        if image.startswith("neg/"):
            errors.append("%s: neg 记录混进了 crops 列表（规范 §10.1）" % tag)
        path = export_root / image
        if not path.is_file():
            errors.append("%s: 图片不存在" % tag)
            continue
        size = rec.get("size") or {}
        actual = png_size(path)
        if actual is None:
            errors.append("%s: 不是合法 PNG" % tag)
        elif (actual[0], actual[1]) != (size.get("w"), size.get("h")):
            errors.append(
                "%s: 实际宽高 %sx%s != size %sx%s（规范 §9-5）"
                % (tag, actual[0], actual[1], size.get("w"), size.get("h"))
            )
        digest = sha1_file(path)
        if digest != rec.get("sha1"):
            errors.append("%s: sha1 %s != 记录 %s（规范 §9-6）" % (tag, digest, rec.get("sha1")))
        rect = rec.get("rect") or {}
        if not all(k in rect for k in ("x", "y", "w", "h")):
            errors.append("%s: rect 字段不完整" % tag)
        if rec.get("polarity") not in POLARITY_VOCAB:
            errors.append("%s: polarity=%r 非法" % (tag, rec.get("polarity")))
        if rec.get("mode") not in MODE_VOCAB:
            errors.append("%s: mode=%r 非法" % (tag, rec.get("mode")))
        if rec.get("origin") not in ORIGIN_VOCAB:
            errors.append("%s: origin=%r 非法" % (tag, rec.get("origin")))
        if not NAME_RE.match(rec.get("screen_id") or ""):
            errors.append("%s: screen_id=%r 不符合命名规则" % (tag, rec.get("screen_id")))
        if not NAME_RE.match(rec.get("tag_id") or ""):
            errors.append("%s: tag_id=%r 不符合命名规则" % (tag, rec.get("tag_id")))
        bad_flags = set(rec.get("flags") or []) - FLAG_VOCAB
        if bad_flags:
            errors.append("%s: flags 含词表外取值 %s" % (tag, sorted(bad_flags)))
        if not normalize_text(rec.get("text") or "", keep_space=True):
            errors.append("%s: text 为空（规范 §6 要求 text = expect 原文）" % tag)

    # §9-8 neg 分支
    files = manifest.get("files") or {}
    if not neg:
        for key in ("neg", "neg_labels_jsonl"):
            if files.get(key) is not None:
                errors.append("files.%s=%r 但 neg_crops 为空（规范 §9-8）" % (key, files.get(key)))

    # §9-10 charset
    if charset:
        used = set()
        for rec in records:
            used |= {ch for ch in (rec.get("text") or "") if not ch.isspace()}
        missing = used - charset
        if missing:
            errors.append("charset.txt 缺少 %d 个字符（规范 §9-10）：%s" % (len(missing), "".join(sorted(missing))))
    else:
        warn("导出目录没有 charset.txt，跳过字符集校验")

    return {"totals": totals, "neg": neg}


# --------------------------------------------------------------------------
# 划分：按 screen_id 分组（同 sha1 归并），保证同屏不跨集
# --------------------------------------------------------------------------
def build_units(records: list):
    """返回 [{key, screens, records}]，同屏与同 sha1 的记录必定同组。"""
    by_screen = {}
    for rec in records:
        by_screen.setdefault(rec["screen_id"], []).append(rec)

    parent = {sid: sid for sid in by_screen}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    shas = {}
    for sid, recs in by_screen.items():
        for rec in recs:
            sha = rec.get("sha1")
            if sha:
                if sha in shas:
                    union(shas[sha], sid)
                else:
                    shas[sha] = sid

    grouped = {}
    for sid in by_screen:
        grouped.setdefault(find(sid), []).append(sid)

    units = []
    for root, sids in grouped.items():
        sids = sorted(sids)
        recs = [r for sid in sids for r in by_screen[sid]]
        units.append({"key": "+".join(sids), "screens": sids, "records": recs})
    units.sort(key=lambda u: u["key"])
    return units


def split_records(records: list, val_ratio: float, val_screens: list, warn):
    """返回 (train_records, val_records, split_info)"""
    units = build_units(records)
    available = {sid for u in units for sid in u["screens"]}

    if val_screens:
        unknown = [s for s in val_screens if s not in available]
        if unknown:
            warn("--val-screens 里的界面不存在，已忽略：%s" % ", ".join(unknown))
        picked_keys = {u["key"] for u in units if set(u["screens"]) & set(val_screens)}
        if not picked_keys:
            warn("--val-screens 未命中任何界面，改为按 --val-ratio 划分")
        else:
            val = [r for u in units if u["key"] in picked_keys for r in u["records"]]
            train = [r for u in units if u["key"] not in picked_keys for r in u["records"]]
            if not train:
                raise SystemExit("[错误] --val-screens 会清空训练集，请少选几个界面")
            return train, val, {
                "rule": "explicit-val-screens",
                "val_units": sorted(picked_keys),
                "train_units": sorted(u["key"] for u in units if u["key"] not in picked_keys),
            }

    if val_ratio <= 0:
        return list(records), [], {"rule": "no-val", "val_units": [], "train_units": [u["key"] for u in units]}

    target = max(1, int(round(len(records) * val_ratio)))
    order = sorted(units, key=lambda u: (len(u["records"]), u["key"]))
    val_units, val_count = [], 0
    for unit in order:
        if val_count >= target:
            break
        if len(val_units) >= len(units) - 1:  # 至少留 1 组给训练
            break
        val_units.append(unit)
        val_count += len(unit["records"])
    val_keys = {u["key"] for u in val_units}
    train = [r for u in units if u["key"] not in val_keys for r in u["records"]]
    val = [r for u in val_units for r in u["records"]]
    if not val:
        warn("数据量太小，无法切出验证集：本次全部作为训练集（评测请另想办法）")
    return train, val, {
        "rule": "val-ratio-by-screen",
        "target_val_records": target,
        "val_units": sorted(val_keys),
        "train_units": sorted(u["key"] for u in units if u["key"] not in val_keys),
    }


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------
def clean_out_dir(out: Path, force: bool):
    if not out.exists():
        return
    entries = sorted(p.name for p in out.iterdir())
    if not entries:
        return
    if not force:
        raise SystemExit(
            "[错误] 输出目录非空：%s\n       加 --force 覆盖（只清理本工具产出的条目：%s），或换一个空目录"
            % (out, ", ".join(OWN_ARTIFACTS))
        )
    unknown = [name for name in entries if name not in OWN_ARTIFACTS]
    if unknown:
        raise SystemExit(
            "[错误] %s 里有不是本工具产出的条目（%s），已拒绝清理以免误删。请换一个空目录。"
            % (out, ", ".join(unknown))
        )
    for name in entries:
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()


def place_image(src: Path, dst: Path, mode: str):
    if mode == "copy":
        shutil.copy2(src, dst)
    elif mode == "hardlink":
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    elif mode == "symlink":
        os.symlink(os.path.abspath(src), dst)
    else:  # pragma: no cover - argparse 已限制
        raise ValueError("未知的 link 模式：%s" % mode)


def write_label_file(path: Path, entries: list, group_same_text: bool):
    """entries: [(相对路径, 文本)]；group_same_text 时同文本合并成一行 JSON 列表。"""
    lines = []
    if group_same_text:
        buckets = {}
        for rel, text in entries:
            buckets.setdefault(text, []).append(rel)
        for text, rels in buckets.items():
            if len(rels) > 1:
                lines.append("%s\t%s" % (json.dumps(rels, ensure_ascii=False), text))
            else:
                lines.append("%s\t%s" % (rels[0], text))
    else:
        for rel, text in entries:
            lines.append("%s\t%s" % (rel, text))
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return len(lines)


def train_command(out_dir: Path, ppocr_config: str) -> str:
    data_dir = out_dir.as_posix()
    return (
        "python tools/train.py -c {cfg} -o \\\n"
        "  Train.dataset.data_dir={d} \\\n"
        "  Train.dataset.label_file_list=['{d}/rec_gt_train.txt'] \\\n"
        "  Eval.dataset.data_dir={d} \\\n"
        "  Eval.dataset.label_file_list=['{d}/rec_gt_test.txt']".format(cfg=ppocr_config, d=data_dir)
    )


def write_readme(out: Path, report: dict, ppocr_config: str):
    s = report["stats"]
    body = [
        "# PPOCR rec 数据（由 prepare_rec_dataset.py 生成）",
        "",
        "- 生成时间：`%s`" % report["generated_at"],
        "- 来源导出：`%s`（%s）" % (report["source"]["export_root"], report["source"].get("tool") or "未知工具"),
        "- 训练集：%d 条 / %d 个界面；验证集：%d 条 / %d 个界面" % (
            s["train_records"], s["train_screens"], s["val_records"], s["val_screens"]),
        "- 划分规则：`%s`（同屏样本不跨集；同 sha1 已归并）" % report["split"]["rule"],
        "",
        "## 目录",
        "",
        "```",
        "rec/",
        "├── rec_gt_train.txt   # 相对路径<TAB>文本，路径相对本目录",
        "├── rec_gt_test.txt",
        "├── train/             # 平铺图片",
        "└── test/",
        "```",
        "",
        "## 训练命令（-o 覆盖，不必改 YAML）",
        "",
        "```bash",
        train_command(out, ppocr_config),
        "```",
        "",
        "## 注意",
        "",
        "- 标签 = 去空白后的文本；`mode=contains/equals` 的判定语义在**评测**阶段生效，训练只用文本本身。",
        "- 微调官方 `PP-OCRv5_server_rec` 时 **dict 必须沿用官方 `ppocrv5_dict.txt`**，不要用 `charset.txt` 替换。",
        "- `neg/` 记录一律未纳入（其裁剪图不含标签文本，混入即错标）。",
        "- 详细统计、逐条映射与告警见 `rec_dataset_report.json`。",
        "",
    ]
    (out / "README.md").write_text("\n".join(body), encoding="utf-8")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def convert(args):
    export_root = Path(args.export).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    errors, warnings = [], []

    def warn(msg):
        warnings.append(msg)
        print("  [告警] %s" % msg, file=sys.stderr)

    if not export_root.is_dir():
        raise SystemExit("[错误] 导出目录不存在：%s" % export_root)
    if out_dir == export_root or export_root in out_dir.parents:
        raise SystemExit("[错误] 输出目录不能位于导出目录内部：%s" % out_dir)

    print("[1/5] 读取导出产物：%s" % export_root)
    data = load_export(export_root, warn)
    manifest = data["manifest"]
    records = data["records"]
    print(
        "      export_schema_version=%s tool=%s 记录=%d neg=%d"
        % (
            manifest.get("export_schema_version"),
            manifest.get("tool"),
            len(records),
            len(manifest.get("neg_crops") or []),
        )
    )

    print("[2/5] 校验（ocr-data-spec.md §9）")
    meta = verify_export(export_root, data, errors, warn)
    for item in errors:
        print("  [错误] %s" % item, file=sys.stderr)
    if errors and not args.allow_invalid:
        raise SystemExit(
            "[中止] 校验未通过 %d 项。修数据后重跑；确要带病转换请加 --allow-invalid。" % len(errors)
        )
    print("      校验%s（%d 项错误 / %d 项告警）" % ("未通过但已放行" if errors else "通过", len(errors), len(warnings)))

    # 过滤
    print("[3/5] 过滤与规范化")
    drop_flags = {f.strip() for f in (args.drop_flags or "").split(",") if f.strip()}
    unknown_flags = drop_flags - FLAG_VOCAB
    if unknown_flags:
        print("  [告警] --drop-flags 含词表外取值：%s" % sorted(unknown_flags), file=sys.stderr)
    dropped = []
    kept = []
    for rec in records:
        flags = set(rec.get("flags") or [])
        if flags & drop_flags:
            dropped.append({"image": rec.get("image"), "text": rec.get("text"),
                            "screen_id": rec.get("screen_id"), "reason": "flag:" + ",".join(sorted(flags & drop_flags))})
            continue
        if rec.get("polarity") != "pos":
            dropped.append({"image": rec.get("image"), "text": rec.get("text"),
                            "screen_id": rec.get("screen_id"), "reason": "polarity:" + str(rec.get("polarity"))})
            continue
        kept.append(rec)
    if not kept:
        raise SystemExit("[错误] 过滤后没有可用记录（检查 --drop-flags）")

    text_changes = []
    normalized = []
    for rec in kept:
        raw = rec.get("text") or ""
        norm = normalize_text(raw, args.keep_space)
        if norm != raw:
            text_changes.append({"image": rec["image"], "before": raw, "after": norm})
        if not norm:
            dropped.append({"image": rec["image"], "text": raw, "screen_id": rec.get("screen_id"),
                            "reason": "empty-text-after-normalize"})
            continue
        normalized.append((rec, norm))
    kept = [rec for rec, _ in normalized]

    fallback = [r["image"] for r in kept if set(r.get("flags") or []) & {"fallback_sample"}]
    if fallback:
        warn("%d 条记录带 fallback_sample，需人工复核：%s" % (len(fallback), ", ".join(fallback[:5])))
    edge = [r["image"] for r in kept if set(r.get("flags") or []) & {"edge_touch"}]
    if edge:
        warn("%d 条记录带 edge_touch（文字可能被框切边），仅提示：%s" % (len(edge), ", ".join(edge[:5])))

    print("[4/5] 划分（按 screen_id，同 sha1 归并）")
    train_records, val_records, split_info = split_records(kept, args.val_ratio, args.val_screens, warn)
    train_text = {id(r): t for r, t in normalized}
    print("      train=%d 条 / val=%d 条（%s）" % (len(train_records), len(val_records), split_info["rule"]))
    if not val_records:
        warn("验证集为空：Eval.dataset.label_file_list 需临时指到 rec_gt_train.txt，或先补标注")

    # 命名与落盘计划
    plan = {"train": [], "test": []}
    used = set()
    record_map = []
    for split_name, recs in (("train", train_records), ("test", val_records)):
        for rec in recs:
            stem = Path(rec.get("sample") or "sample").stem
            base = sanitize_name("%s__%s__%s.png" % (rec.get("screen_id"), rec.get("tag_id"), stem))
            name, n = base, 2
            while (split_name, name) in used:
                name = base[:-4] + "-%d.png" % n
                n += 1
            used.add((split_name, name))
            rel = "%s/%s" % (split_name, name)
            text = train_text[id(rec)]
            plan[split_name].append((rel, text, rec))
            record_map.append(
                {
                    "split": split_name,
                    "image": rel,
                    "source_image": rec.get("image"),
                    "text": text,
                    "text_original": rec.get("text"),
                    "screen_id": rec.get("screen_id"),
                    "tag_id": rec.get("tag_id"),
                    "sample": rec.get("sample"),
                    "mode": rec.get("mode"),
                    "origin": rec.get("origin"),
                    "sha1": rec.get("sha1"),
                    "flags": rec.get("flags") or [],
                }
            )

    stats = {
        "records_in_export": len(records),
        "records_used": len(kept),
        "records_dropped": len(dropped),
        "train_records": len(plan["train"]),
        "val_records": len(plan["test"]),
        "train_screens": len({r["screen_id"] for _, _, r in plan["train"]}),
        "val_screens": len({r["screen_id"] for _, _, r in plan["test"]}),
        "text_normalized": len(text_changes),
        "neg_skipped": len(manifest.get("neg_crops") or []),
        "label_lines_train": 0,
        "label_lines_test": 0,
    }

    if args.dry_run:
        print("[5/5] --dry-run：不写任何文件")
        print("      计划 train=%d 条，test=%d 条，输出目录 %s" % (stats["train_records"], stats["val_records"], out_dir))
        return {"stats": stats, "dropped": dropped, "dry_run": True, "split": split_info}

    clean_out_dir(out_dir, args.force)
    print("[5/5] 写出：%s" % out_dir)
    (out_dir / "train").mkdir(parents=True, exist_ok=True)
    if plan["test"]:
        (out_dir / "test").mkdir(parents=True, exist_ok=True)

    for split_name in ("train", "test"):
        for rel, _text, rec in plan[split_name]:
            src = export_root / rec["image"]
            dst = out_dir / rel
            if args.link == "symlink" and not src.is_file():
                raise SystemExit("[错误] 源图不存在：%s" % src)
            place_image(src, dst, args.link)

    stats["label_lines_train"] = write_label_file(
        out_dir / "rec_gt_train.txt", [(rel, text) for rel, text, _ in plan["train"]], args.group_same_text
    )
    stats["label_lines_test"] = write_label_file(
        out_dir / "rec_gt_test.txt", [(rel, text) for rel, text, _ in plan["test"]], args.group_same_text
    )

    if args.write_charset:
        chars = sorted({ch for _, text, _ in plan["train"] for ch in text if not ch.isspace()})
        (out_dir / "charset.txt").write_text("".join(ch + "\n" for ch in chars), encoding="utf-8")
        stats["charset_chars"] = len(chars)

    report = {
        "generated_at": now_iso(),
        "tool": "prepare_rec_dataset.py",
        "source": {
            "export_root": str(export_root),
            "tool": manifest.get("tool"),
            "export_schema_version": manifest.get("export_schema_version"),
            "generated_at": manifest.get("generated_at"),
            "manifest_files": data["files"],
            "export_options": manifest.get("options"),
            "export_totals": meta.get("totals"),
        },
        "options": {
            "out": str(out_dir),
            "val_ratio": args.val_ratio,
            "val_screens": args.val_screens,
            "drop_flags": sorted(drop_flags),
            "keep_space": args.keep_space,
            "group_same_text": args.group_same_text,
            "link": args.link,
            "write_charset": args.write_charset,
        },
        "split": split_info,
        "stats": stats,
        "dropped": dropped,
        "text_changes": text_changes,
        "records": record_map,
        "warnings": warnings,
        "errors": errors,
        "files": {
            "rec_gt_train": "rec_gt_train.txt",
            "rec_gt_test": "rec_gt_test.txt",
            "train_dir": "train/",
            "test_dir": "test/" if plan["test"] else None,
            "report": "rec_dataset_report.json",
            "readme": "README.md",
            "charset": "charset.txt" if args.write_charset else None,
        },
        "train_command": train_command(out_dir, args.ppocr_config),
    }
    (out_dir / "rec_dataset_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_readme(out_dir, report, args.ppocr_config)

    print("")
    print("完成：train %d 条（%d 界面）/ val %d 条（%d 界面）；丢弃 %d 条；文本规范化 %d 条"
          % (stats["train_records"], stats["train_screens"], stats["val_records"], stats["val_screens"],
             stats["records_dropped"], stats["text_normalized"]))
    print("  标签：%s" % (out_dir / "rec_gt_train.txt"))
    print("        %s" % (out_dir / "rec_gt_test.txt"))
    print("  报告：%s" % (out_dir / "rec_dataset_report.json"))
    print("")
    print("训练命令（-o 覆盖）：")
    print(train_command(out_dir, args.ppocr_config))
    print("")
    print("提示：微调 PP-OCRv5_server_rec 时 character_dict_path 必须沿用官方 ppocrv5_dict.txt，")
    print("      不要用 charset.txt 替换（换 dict 会改变输出层类别数）。")
    if errors:
        print("注意：本次带 --allow-invalid 转换，报告里 errors 非空，产物仅供调试。")
    return report


# --------------------------------------------------------------------------
# 自检：不依赖真实数据集
# --------------------------------------------------------------------------
def _png_bytes(w: int, h: int, rgb=(200, 40, 40)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        PNG_SIG
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _fake_export(root: Path):
    """造一个最小但完全合规的导出产物。"""
    specs = [
        ("jianbao", "t1", "jianbao_001", "率土简报", (60, 20)),
        ("shezhi", "t1", "shezhi_001", "系统设置", (50, 18)),
        ("zhujiemian", "t1", "zhujiemian_001", "武将", (30, 40)),
        ("zhujiemian", "t2", "zhujiemian_001", "库藏", (30, 40)),
        ("tishi_xiazaiziyuan", "t1", "tishi_xiazaiziyuan_001", "确定", (40, 16)),
        ("tishi_xiazaiziyuan", "t2", "tishi_xiazaiziyuan_001", "取消", (40, 16)),
    ]
    crops, neg_crops, screens = [], [], {}
    for screen_id, tag_id, stem, text, (w, h) in specs:
        rel = "crops/%s/%s__%s.png" % (screen_id, tag_id, stem)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = _png_bytes(w, h)
        path.write_bytes(blob)
        rec = {
            "image": rel,
            "text": text,
            "screen_id": screen_id,
            "tag_id": tag_id,
            "sample": "shots/%s.png" % stem,
            "polarity": "pos",
            "mode": "contains",
            "box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05},
            "rect": {"x": 10, "y": 20, "w": w, "h": h},
            "size": {"w": w, "h": h},
            "origin": "reference",
            "sha1": hashlib.sha1(blob).hexdigest(),
            "ink_ratio": 0.3,
            "flags": [],
        }
        crops.append(rec)
        entry = screens.setdefault(screen_id, {"screen_id": screen_id, "display_name": screen_id,
                                               "transient": False, "tags": 0, "samples": 1,
                                               "crops": 0, "neg_crops": 0})
        entry["tags"] += 1
        entry["crops"] += 1

    crops[-1]["flags"] = ["edge_touch"]
    charset = sorted({ch for spec in specs for ch in spec[3]})
    (root / "charset.txt").write_text("".join(c + "\n" for c in charset), encoding="utf-8")
    manifest = {
        "export_schema_version": 1,
        "tool": "ui-annotator@self-test",
        "generated_at": now_iso(),
        "dataset_root": "/tmp/dataset",
        "output_root": str(root),
        "dry_run": False,
        "options": {"scope": "source", "pad_px": 0, "pad_ratio": 0.0, "resize_height": 0,
                    "include_neg": False, "only_screens": []},
        "totals": {"screens": len(screens), "failed_screens": 0, "crops": len(crops), "neg_crops": 0,
                   "skipped_neg": 0, "warnings": 0, "flagged": 1},
        "files": {"labels": "labels.tsv", "labels_jsonl": "labels.jsonl", "charset": "charset.txt",
                  "crops": "crops/", "neg_labels_jsonl": None, "neg": None},
        "screens": list(screens.values()),
        "warnings": [],
        "errors": [],
        "crops": crops,
        "neg_crops": neg_crops,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "labels.tsv").write_text("".join("%s\t%s\n" % (r["image"], r["text"]) for r in crops), encoding="utf-8")
    (root / "labels.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in crops), encoding="utf-8"
    )


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="rec-selftest-") as tmp:
        tmp_path = Path(tmp)
        export_root = tmp_path / "export"
        out_dir = tmp_path / "out"
        export_root.mkdir()
        _fake_export(export_root)
        print("== 自检：合成导出产物 %s ==" % export_root)
        args = argparse.Namespace(
            export=str(export_root), out=str(out_dir), val_ratio=0.34, val_screens=[],
            drop_flags="blank", keep_space=False, group_same_text=False, link="copy",
            write_charset=True, dry_run=False, force=False, allow_invalid=False,
            ppocr_config=DEFAULT_PPOCR_CONFIG,
        )
        report = convert(args)
        stats = report["stats"]
        problems = []
        train_lines = read_lines(out_dir / "rec_gt_train.txt")
        test_lines = read_lines(out_dir / "rec_gt_test.txt")
        if len(train_lines) != stats["train_records"]:
            problems.append("rec_gt_train.txt 行数 != 训练记录数")
        if len(test_lines) != stats["val_records"]:
            problems.append("rec_gt_test.txt 行数 != 验证记录数")
        for line in train_lines + test_lines:
            path_part, text = line.split("\t")
            if not (out_dir / path_part).is_file():
                problems.append("标签引用的图片不存在：%s" % path_part)
            if "\t" in text or "\n" in text or not text:
                problems.append("标签文本非法：%r" % text)
        train_screens = {r["screen_id"] for r in report["records"] if r["split"] == "train"}
        test_screens = {r["screen_id"] for r in report["records"] if r["split"] == "test"}
        if train_screens & test_screens:
            problems.append("同屏样本跨集：%s" % sorted(train_screens & test_screens))
        if not (out_dir / "rec_dataset_report.json").is_file():
            problems.append("缺少 rec_dataset_report.json")
        if problems:
            print("自检失败：")
            for p in problems:
                print("  - %s" % p)
            return EXIT_FAIL
        print("\n自检通过：train %d 条 / val %d 条，同屏不跨集，标签格式合规（相对路径<TAB>文本）"
              % (stats["train_records"], stats["val_records"]))
        return EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把 ui-annotator 的 OCR 导出产物转成 PPOCR 识别（rec）训练 / 测试数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 src/tools/prepare_rec_dataset.py --export /mnt/d/ui-dataset/ocr_export \\\n"
            "      --out /mnt/d/ui-dataset/ppocr/rec --val-ratio 0.2\n"
            "  python3 src/tools/prepare_rec_dataset.py --self-test\n"
        ),
    )
    p.add_argument("-e", "--export", help="ui-annotator 导出根目录（含 manifest.json / crops/）")
    p.add_argument("-o", "--out", help="输出目录（默认 <导出目录同级>/ppocr_rec）")
    p.add_argument("--val-ratio", type=float, default=0.2,
                   help="验证集记录占比，按界面（组）挑选；0 = 不切验证集（默认 0.2）")
    p.add_argument("--val-screens", default="",
                   help="显式指定作为验证集的界面，逗号分隔（如 zhujiemian,jianbao），优先于 --val-ratio")
    p.add_argument("--drop-flags", default="blank",
                   help="按质检标记剔除记录，逗号分隔（默认 blank；词表：%s）" % ",".join(sorted(FLAG_VOCAB)))
    p.add_argument("--keep-space", action="store_true",
                   help="保留文本内部空格（默认去掉全部空白，与 ocr-data-spec.md §10.2 一致）")
    p.add_argument("--group-same-text", action="store_true",
                   help="同文本多图合并成一行 JSON 列表（PPOCR 离线增广采样）")
    p.add_argument("--link", choices=["copy", "hardlink", "symlink"], default="copy",
                   help="图片落到 train/ test/ 的方式（默认 copy）")
    p.add_argument("--write-charset", action="store_true", help="额外写 charset.txt（训练集字符集，仅供参考）")
    p.add_argument("--force", action="store_true", help="输出目录非空时先清理（只清理本工具产出的条目）")
    p.add_argument("--dry-run", action="store_true", help="只统计与校验，不写文件")
    p.add_argument("--allow-invalid", action="store_true", help="校验失败也继续（报告里 errors 非空，仅供调试）")
    p.add_argument("--ppocr-config", default=DEFAULT_PPOCR_CONFIG,
                   help="打印训练命令时用的 PPOCR rec 配置文件（默认 %s）" % DEFAULT_PPOCR_CONFIG)
    p.add_argument("--self-test", action="store_true", help="用合成产物跑一遍自检，不需要真实数据集")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if not args.export:
        parser.error("缺少 --export（或使用 --self-test）")
    if not args.out:
        args.out = str(Path(args.export).expanduser().resolve().parent / "ppocr_rec")
    args.val_screens = [s.strip() for s in (args.val_screens or "").split(",") if s.strip()]
    if not 0.0 <= args.val_ratio < 1.0:
        parser.error("--val-ratio 必须在 [0, 1) 之间")

    try:
        convert(args)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
