#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐条 crop 评测 OCR 服务：对着 ui-annotator 的 labels.tsv/jsonl 算逐条准确率与耗时分布。

流程：读导出产物 → 一条一条（串行，不并发）把 crop 发 POST /ocr → 按 mode 比对 → 出报告。

比对规则（docs/ocr-data-spec.md §10.2 / §11）：
  actual = 该图所有识别块的 text 去掉全部空白后拼接
  contains: expect 是 actual 的子串        equals: 归一化后全等
空白归一化是硬要求：服务端会把一句话拆成多个 text 块（整屏实测复现过）。

用法：
    python3 src/tools/eval_rec_service.py --export /mnt/d/ui-dataset/ocr_export
    python3 src/tools/eval_rec_service.py --export /mnt/d/ui-dataset/ocr_export --limit 3
    python3 src/tools/eval_rec_service.py --labels-file train_data/rec/rec_gt_train.txt \
        --data-dir train_data/rec
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_NOT_ALL_PASSED = 1    # 有未通过（识别错或请求失败）
EXIT_USAGE = 2             # 用法/输入/服务不可用
EXIT_ACCURACY_GATE = 3     # --fail-under 未达标

DEFAULT_URL = "http://192.168.50.2:8118/ocr"
DEFAULT_TIMEOUT = 60.0
HIST_BUCKETS = [(0, 50), (50, 100), (100, 200), (200, 500), (500, 1000), (1000, 2000), (2000, None)]
MODE_VOCAB = {"contains", "equals"}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm_text(text: str) -> str:
    """去掉全部空白（制表符/换行/各种空格）。"""
    return "".join(text.split())


def read_lines(path: Path):
    text = path.read_text(encoding="utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n") if text else []


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def fmt_ms(v):
    return "—" if v is None else ("%.0f ms" % v if v < 1000 else "%.2f s" % (v / 1000.0))


def unique_path(out_dir: Path, stem: str, suffix: str) -> Path:
    """同一秒内多次运行也不互相覆盖报告。"""
    path = out_dir / (stem + suffix)
    n = 2
    while path.exists():
        path = out_dir / ("%s-%d%s" % (stem, n, suffix))
        n += 1
    return path


# --------------------------------------------------------------------------
# 输入：ui-annotator 导出产物 / PPOCR 标签文件
# --------------------------------------------------------------------------
def load_from_export(export_root: Path, warn):
    mpath = export_root / "manifest.json"
    if not mpath.is_file():
        raise SystemExit(
            "[错误] 找不到 %s —— --export 需指向 ui-annotator 导出根目录（含 manifest.json / labels.tsv / crops/）"
            % mpath
        )
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    if manifest.get("export_schema_version") != 1:
        raise SystemExit(
            "[错误] export_schema_version=%r，本脚本只认 1（docs/ocr-data-spec.md §12）"
            % manifest.get("export_schema_version")
        )
    records = list(manifest.get("crops") or [])
    jl = export_root / "labels.jsonl"
    if jl.is_file():
        rows = [json.loads(line) for line in read_lines(jl) if line.strip()]
        if len(rows) == len(records):
            records = rows
        else:
            warn("labels.jsonl 行数(%d) != manifest.crops(%d)，用 manifest" % (len(rows), len(records)))

    tsv = export_root / "labels.tsv"
    if tsv.is_file():
        rows = [line.split("\t") for line in read_lines(tsv) if line != ""]
        if [r[0] for r in rows] != [r.get("image") for r in records]:
            warn("labels.tsv 与记录集合/顺序不一致（规范 §9-4），本次以 labels.jsonl 为准")

    items = []
    for rec in records:
        if rec.get("polarity") != "pos":
            continue
        items.append(
            {
                "image": str(export_root / rec["image"]),
                "image_rel": rec["image"],
                "expect": rec.get("text") or "",
                "mode": rec.get("mode") if rec.get("mode") in MODE_VOCAB else "contains",
                "screen_id": rec.get("screen_id"),
                "tag_id": rec.get("tag_id"),
                "flags": rec.get("flags") or [],
                "comment": rec.get("comment"),
                "source": "export",
            }
        )
    meta = {
        "kind": "ui-annotator-export",
        "path": str(export_root),
        "tool": manifest.get("tool"),
        "export_schema_version": manifest.get("export_schema_version"),
        "exported_at": manifest.get("generated_at"),
        "neg_skipped": len(manifest.get("neg_crops") or []),
    }
    return items, meta


def load_from_labels(label_file: Path, data_dir: Path):
    """PPOCR 形式的标签文件：相对路径<TAB>文本（mode 未知 → 一律 contains）。"""
    items = []
    for line in read_lines(label_file):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise SystemExit("[错误] %s 有一行不是 2 列：%r" % (label_file, line))
        rel, text = parts
        if rel.startswith("["):  # PPOCR 离线增广的多图行：本脚本只评测第一张
            rel = json.loads(rel)[0]
        path = (data_dir / rel) if not Path(rel).is_absolute() else Path(rel)
        items.append(
            {
                "image": str(path),
                "image_rel": rel,
                "expect": text,
                "mode": "contains",
                "screen_id": None,
                "tag_id": None,
                "flags": [],
                "comment": None,
                "source": "labels-file",
            }
        )
    meta = {"kind": "ppocr-labels", "path": str(label_file), "data_dir": str(data_dir)}
    return items, meta


# --------------------------------------------------------------------------
# 进度条（纯 ASCII，兼容 Windows 控制台）
# --------------------------------------------------------------------------
class Progress:
    def __init__(self, total, width=28, stream=sys.stderr):
        self.total = total
        self.width = width
        self.stream = stream
        self.start = time.time()
        self.enabled = total > 0
        self.last_len = 0

    def update(self, done, note=""):
        if not self.enabled:
            return
        frac = done / self.total
        filled = int(frac * self.width)
        bar = "#" * filled + "-" * (self.width - filled)
        elapsed = time.time() - self.start
        eta = (elapsed / done) * (self.total - done) if done else 0.0
        line = "  [%s] %d/%d %3d%% | 已用 %s | 预计剩余 %s%s" % (
            bar, done, self.total, int(frac * 100), fmt_ms(elapsed * 1000), fmt_ms(eta * 1000),
            ("  " + note) if note else "",
        )
        pad = max(0, self.last_len - len(line))
        self.stream.write("\r" + line + " " * pad)
        self.stream.flush()
        self.last_len = len(line)

    def close(self):
        if self.enabled:
            self.stream.write("\n")
            self.stream.flush()


# --------------------------------------------------------------------------
# 调服务
# --------------------------------------------------------------------------
def http_json(url: str, payload, timeout: float):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_service(url: str, timeout: float):
    """GET / 探活：404/405 也算在监听（该路径无路由）。返回 (ok, 说明)"""
    base = url.rsplit("/", 1)[0] + "/"
    try:
        with urllib.request.urlopen(base, timeout=min(timeout, 5.0)) as resp:
            return True, "HTTP %d" % resp.status
    except urllib.error.HTTPError as exc:
        return True, "HTTP %d（/ 无路由属正常）" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def predict_one(url: str, image_path: Path, timeout: float, retries: int):
    """返回 (words, latency_ms, error_or_None)"""
    try:
        blob = image_path.read_bytes()
    except OSError as exc:
        return None, 0.0, "读图失败: %s" % exc
    payload = {"image": base64.b64encode(blob).decode("ascii"), "image_type": image_path.suffix.lstrip(".")}
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        try:
            data = http_json(url, payload, timeout)
            latency = (time.perf_counter() - t0) * 1000.0
            if data.get("code") != 0:
                return None, latency, "服务返回 code=%r message=%r" % (data.get("code"), data.get("message"))
            words = (data.get("data") or {}).get("words") or []
            return words, latency, None
        except urllib.error.HTTPError as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last_err = ("HTTP %d %s" % (exc.code, detail)).strip(), latency
        except Exception as exc:  # noqa: BLE001
            latency = (time.perf_counter() - t0) * 1000.0
            last_err = ("%s: %s" % (type(exc).__name__, exc), latency)
        if attempt < retries:
            time.sleep(0.5)
    return None, last_err[1], last_err[0]


# --------------------------------------------------------------------------
# 评测
# --------------------------------------------------------------------------
def evaluate(items, url, timeout, retries, conf_threshold, progress, verbose):
    results = []
    for idx, item in enumerate(items, 1):
        words, latency, error = predict_one(url, Path(item["image"]), timeout, retries)

        blocks = []
        for w in words or []:
            blocks.append({"text": w.get("text", ""), "confidence": float(w.get("confidence", 0.0))})

        kept = [b for b in blocks if b["confidence"] >= conf_threshold]
        actual_raw = "\n".join(b["text"] for b in kept)
        actual = norm_text(actual_raw)
        expect_norm = norm_text(item["expect"])

        if error:
            matched, reason = False, "请求错误: %s" % error
        elif item["mode"] == "equals":
            matched = actual == expect_norm
            reason = None if matched else "equals 不等"
        else:
            matched = expect_norm in actual
            reason = None if matched else "contains 未命中"

        results.append(
            {
                "index": idx,
                "image": item["image"],
                "image_rel": item["image_rel"],
                "screen_id": item["screen_id"],
                "tag_id": item["tag_id"],
                "flags": item["flags"],
                "mode": item["mode"],
                "expect": item["expect"],
                "expect_norm": expect_norm,
                "actual": actual,
                "actual_raw": actual_raw,
                "blocks": kept,
                "blocks_dropped_by_conf": len(blocks) - len(kept),
                "matched": matched,
                "reason": reason,
                "error": error,
                "latency_ms": round(latency, 1),
            }
        )
        if verbose:
            sys.stdout.write(
                "  %s %-58s %-10s %s\n"
                % ("PASS" if matched else "FAIL", item["image_rel"], fmt_ms(latency),
                   (item["expect"] if matched else "期望=%r 实得=%r" % (item["expect"], actual or actual_raw)))
            )
            sys.stdout.flush()
        progress.update(idx, "最近 %s" % fmt_ms(latency))

    return results


def summarize(results, warmup_ms, conf_threshold):
    latencies = [r["latency_ms"] for r in results if not r["error"]]
    errors = [r for r in results if r["error"]]
    passed = [r for r in results if r["matched"]]
    failed = [r for r in results if not r["matched"] and not r["error"]]

    def acc(rows):
        return (len([r for r in rows if r["matched"]]) / len(rows) * 100.0) if rows else None

    by_mode = {}
    for mode in sorted({r["mode"] for r in results}):
        rows = [r for r in results if r["mode"] == mode]
        by_mode[mode] = {"total": len(rows), "passed": len([r for r in rows if r["matched"]]),
                         "accuracy": acc(rows)}

    by_screen = {}
    for r in results:
        key = r["screen_id"] or "(未知)"
        entry = by_screen.setdefault(key, {"total": 0, "passed": 0})
        entry["total"] += 1
        entry["passed"] += 1 if r["matched"] else 0
    for key, entry in by_screen.items():
        entry["accuracy"] = entry["passed"] / entry["total"] * 100.0

    hist = []
    for lo, hi in HIST_BUCKETS:
        rows = [r for r in results if not r["error"] and r["latency_ms"] >= lo and (hi is None or r["latency_ms"] < hi)]
        hist.append({"range": ("%d+" % lo) if hi is None else ("%d-%d" % (lo, hi)),
                     "count": len(rows), "ratio": (len(rows) / len(results) * 100.0) if results else 0.0})

    slowest = sorted([r for r in results if not r["error"]], key=lambda r: -r["latency_ms"])[:5]

    return {
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "errored": len(errors),
        "accuracy": acc(results),
        "accuracy_excluding_errors": acc([r for r in results if not r["error"]]),
        "by_mode": by_mode,
        "by_screen": by_screen,
        "latency": {
            "count": len(latencies),
            "warmup_ms": None if warmup_ms is None else round(warmup_ms, 1),
            "min_ms": round(min(latencies), 1) if latencies else None,
            "p50_ms": round(percentile(latencies, 0.50), 1) if latencies else None,
            "p90_ms": round(percentile(latencies, 0.90), 1) if latencies else None,
            "p95_ms": round(percentile(latencies, 0.95), 1) if latencies else None,
            "max_ms": round(max(latencies), 1) if latencies else None,
            "mean_ms": round(statistics.fmean(latencies), 1) if latencies else None,
            "stdev_ms": round(statistics.pstdev(latencies), 1) if len(latencies) > 1 else 0.0,
            "total_ms": round(sum(latencies), 1),
            "histogram": hist,
        },
        "slowest": slowest,
        "conf_threshold": conf_threshold,
    }


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def bar(count, total, width=24):
    if not total:
        return ""
    filled = int(round(count / total * width))
    return "#" * filled + "." * (width - filled)


def write_markdown(path: Path, summary, results, meta, run_meta):
    s = summary
    lat = s["latency"]
    acc = s["accuracy"]
    lines = []
    lines.append("# OCR 识别评测报告（逐条 crop）")
    lines.append("")
    lines.append("- 生成时间：`%s`" % run_meta["finished_at"])
    lines.append("- 服务：`%s`（探测：%s）" % (run_meta["url"], run_meta["probe"]))
    lines.append("- 数据源：%s `%s`" % (meta.get("kind"), meta.get("path")))
    if meta.get("tool"):
        lines.append("- 导出工具：`%s`（导出格式 v%s，导出时间 %s）"
                     % (meta.get("tool"), meta.get("export_schema_version"), meta.get("exported_at")))
    if meta.get("neg_skipped"):
        lines.append("- 已跳过 neg 记录：%d 条（规范 §10.1，neg 不参与识别评测）" % meta["neg_skipped"])
    lines.append("- 比对规则：`actual = 各识别块文本去空白后拼接`；`contains`=子串命中，`equals`=归一化后全等")
    lines.append("- 置信度阈值：%s | 串行逐条请求（不并发） | 超时 %.0fs | 重试 %d 次"
                 % (("%.2f" % s["conf_threshold"]) if s["conf_threshold"] else "不过滤（0）",
                    run_meta["timeout"], run_meta["retries"]))
    lines.append("")
    lines.append("## 一、总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append("| 总条数 | %d |" % s["total"])
    lines.append("| 通过 | %d |" % s["passed"])
    lines.append("| 不通过（识别错） | %d |" % s["failed"])
    lines.append("| 请求失败（超时/HTTP/服务错） | %d |" % s["errored"])
    lines.append("| **准确率**（通过/总条数） | **%s** |" % ("%.2f%%" % acc if acc is not None else "—"))
    lines.append("| 准确率（排除请求失败） | %s |"
                 % ("%.2f%%" % s["accuracy_excluding_errors"] if s["accuracy_excluding_errors"] is not None else "—"))
    lines.append("")
    lines.append("按比对模式：")
    lines.append("")
    lines.append("| mode | 条数 | 通过 | 准确率 |")
    lines.append("|---|---|---|---|")
    for mode, entry in sorted(s["by_mode"].items()):
        lines.append("| `%s` | %d | %d | %s |"
                     % (mode, entry["total"], entry["passed"],
                        "%.2f%%" % entry["accuracy"] if entry["accuracy"] is not None else "—"))
    lines.append("")
    lines.append("按界面：")
    lines.append("")
    lines.append("| screen_id | 条数 | 通过 | 准确率 |")
    lines.append("|---|---|---|---|")
    for screen_id, entry in sorted(s["by_screen"].items(), key=lambda kv: (-kv[1]["total"], kv[0])):
        lines.append("| `%s` | %d | %d | %.2f%% |" % (screen_id, entry["total"], entry["passed"], entry["accuracy"]))
    lines.append("")
    lines.append("## 二、耗时分布（不含请求失败，单位 ms）")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    if lat["warmup_ms"] is not None:
        lines.append("| 预热请求（不计入统计） | %s |" % fmt_ms(lat["warmup_ms"]))
    for key, label in (("count", "样本数(有效)"), ("min_ms", "最小"), ("p50_ms", "P50 中位"),
                       ("p90_ms", "P90"), ("p95_ms", "P95"), ("max_ms", "最大"),
                       ("mean_ms", "均值"), ("stdev_ms", "标准差"), ("total_ms", "合计")):
        value = lat.get(key)
        lines.append("| %s | %s |" % (label, fmt_ms(value) if key.endswith("_ms") else value))
    lines.append("")
    lines.append("直方图：")
    lines.append("")
    lines.append("```")
    for bucket in lat["histogram"]:
        lines.append("%-10s %s %3d 条 (%5.1f%%)"
                     % (bucket["range"], bar(bucket["count"], s["total"]), bucket["count"], bucket["ratio"]))
    lines.append("```")
    lines.append("")
    if s["slowest"]:
        lines.append("最慢 %d 条：" % len(s["slowest"]))
        lines.append("")
        lines.append("| # | 图 | 耗时 | 期望 | 结果 |")
        lines.append("|---|---|---|---|---|")
        for r in s["slowest"]:
            lines.append("| %d | `%s` | %s | %s | %s |"
                         % (r["index"], r["image_rel"], fmt_ms(r["latency_ms"]), r["expect"],
                            "通过" if r["matched"] else "不通过"))
        lines.append("")
    fails = [r for r in results if not r["matched"]]
    lines.append("## 三、失败清单（%d 条）" % len(fails))
    lines.append("")
    if not fails:
        lines.append("无失败：全部 %d 条通过。" % s["total"])
    else:
        lines.append("| # | 图 | mode | 期望 | 实得（去空白） | 原因 | 耗时 | flags |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in fails:
            shown = r["actual"] if r["actual"] else ("（空" + ("，请求失败" if r["error"] else "）"))
            lines.append("| %d | `%s` | `%s` | %s | %s | %s | %s | %s |"
                         % (r["index"], r["image_rel"], r["mode"], r["expect"], shown,
                            r["reason"] or "不匹配", fmt_ms(r["latency_ms"]),
                            ",".join(r["flags"]) or "-"))
        lines.append("")
        lines.append("原始识别块（便于定位是切块还是识错）：")
        lines.append("")
        lines.append("```")
        for r in fails:
            lines.append("#%d %s" % (r["index"], r["image_rel"]))
            lines.append("   期望: %r" % r["expect"])
            if r["error"]:
                lines.append("   错误: %s" % r["error"])
            for b in r["blocks"]:
                lines.append("   识别: %r (conf=%.4f)" % (b["text"], b["confidence"]))
            if r["blocks_dropped_by_conf"]:
                lines.append("   （另有 %d 块因置信度阈值被丢弃）" % r["blocks_dropped_by_conf"])
        lines.append("```")
    lines.append("")
    lines.append("## 四、逐条证据（TagEvidence 式）")
    lines.append("")
    lines.append("| # | screen_id | tag_id | 期望 | 实得 | mode | 命中 | 耗时 | 块数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        lines.append("| %d | %s | %s | %s | %s | `%s` | %s | %s | %d |"
                     % (r["index"], r["screen_id"] or "-", r["tag_id"] or "-", r["expect"],
                        r["actual"] or "（空）", r["mode"], "✅" if r["matched"] else "❌",
                        fmt_ms(r["latency_ms"]), len(r["blocks"])))
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="逐条 crop 评测 OCR 服务（对着 labels.tsv/jsonl 算准确率与耗时分布）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 src/tools/eval_rec_service.py --export /mnt/d/ui-dataset/ocr_export\n"
            "  python3 src/tools/eval_rec_service.py --export /mnt/d/ui-dataset/ocr_export --limit 3 --verbose\n"
        ),
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("-e", "--export", help="ui-annotator 导出根目录（推荐：含 mode/screen_id/flags 全字段）")
    src.add_argument("-l", "--labels-file", help="PPOCR 风格标签文件（相对路径<TAB>文本，mode 一律按 contains）")
    parser.add_argument("--data-dir", help="--labels-file 时图片根目录（默认标签文件所在目录）")
    parser.add_argument("-u", "--url", default=DEFAULT_URL, help="OCR 接口（默认 %s）" % DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单次请求超时秒（默认 60）")
    parser.add_argument("--retries", type=int, default=0, help="单条失败后的重试次数（默认 0，服务无重试语义）")
    parser.add_argument("--conf-threshold", type=float, default=0.0,
                        help="丢弃置信度低于该值的识别块（默认 0 = 全保留）")
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条（冒烟用，默认全部）")
    parser.add_argument("--warmup", type=int, default=1, help="评测前先发的预热请求数（不计耗时，默认 1）")
    parser.add_argument("--out", default="build/eval", help="报告输出目录（默认 build/eval，已 gitignore）")
    parser.add_argument("--skip-probe", action="store_true", help="跳过服务探活")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="准确率低于该百分比时退出码 3（用于卡回归门限）")
    parser.add_argument("--verbose", action="store_true", help="逐条打印结果（进度条走 stderr，不冲突）")
    args = parser.parse_args(argv)

    warn = lambda msg: print("  [告警] %s" % msg, file=sys.stderr)  # noqa: E731

    if args.export:
        items, meta = load_from_export(Path(args.export).expanduser().resolve(), warn)
    else:
        label_file = Path(args.labels_file).expanduser().resolve()
        if not label_file.is_file():
            parser.error("--labels-file 不存在：%s" % label_file)
        data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else label_file.parent
        items, meta = load_from_labels(label_file, data_dir)

    if not items:
        raise SystemExit("[错误] 没有可评测的 pos 记录")
    if args.limit:
        items = items[: args.limit]

    missing = [it["image"] for it in items if not Path(it["image"]).is_file()]
    if missing:
        raise SystemExit("[错误] %d 张图片不存在，例如：%s" % (len(missing), missing[0]))

    print("OCR 服务评测：%d 条（串行逐条）" % len(items))
    print("  服务   : %s" % args.url)
    print("  数据源 : %s" % meta.get("path"))
    print("  比对   : 去空白后拼接 → contains 子串 / equals 全等")
    if not args.skip_probe:
        ok, note = probe_service(args.url, args.timeout)
        print("  探活   : %s" % note)
        if not ok:
            print("[错误] 服务不可达：%s\n       先确认容器在跑：ssh -F ~/.ssh/config localserver "
                  "'podman ps' 或 bash start_service.sh" % args.url, file=sys.stderr)
            return EXIT_USAGE
    else:
        print("  探活   : 已跳过")

    warmup_ms = None
    for _ in range(max(0, args.warmup)):
        _, warmup_ms, err = predict_one(args.url, Path(items[0]["image"]), args.timeout, 0)
        if err:
            print("[错误] 预热请求失败：%s" % err, file=sys.stderr)
            return EXIT_USAGE

    started = time.time()
    progress = Progress(len(items))
    progress.update(0, "开始")
    results = evaluate(items, args.url, args.timeout, args.retries, args.conf_threshold, progress, args.verbose)
    progress.close()
    elapsed = time.time() - started

    summary = summarize(results, warmup_ms, args.conf_threshold)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    json_path = unique_path(out_dir, "eval_report_%s" % stamp, ".json")
    md_path = json_path.with_suffix(".md")
    run_meta = {
        "started_at": datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds"),
        "finished_at": now_iso(),
        "wall_seconds": round(elapsed, 2),
        "url": args.url,
        "probe": "skipped" if args.skip_probe else "ok",
        "timeout": args.timeout,
        "retries": args.retries,
        "warmup_requests": args.warmup,
        "limit": args.limit or None,
        "verbose": args.verbose,
    }
    json_path.write_text(
        json.dumps({"run": run_meta, "source": meta, "summary": summary, "results": results},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(md_path, summary, results, meta, run_meta)

    s = summary
    print("")
    print("结果：通过 %d / %d，准确率 %s（不通过 %d，请求失败 %d）"
          % (s["passed"], s["total"],
             "%.2f%%" % s["accuracy"] if s["accuracy"] is not None else "—", s["failed"], s["errored"]))
    print("耗时：P50 %s | P90 %s | 最大 %s | 合计 %s（墙钟 %.1fs）"
          % (fmt_ms(s["latency"]["p50_ms"]), fmt_ms(s["latency"]["p90_ms"]),
             fmt_ms(s["latency"]["max_ms"]), fmt_ms(s["latency"]["total_ms"]), elapsed))
    print("报告：%s" % md_path)
    print("      %s" % json_path)
    if s["failed"] or s["errored"]:
        print("")
        print("失败 %d 条（详见报告第三节）：" % (s["failed"] + s["errored"]))
        for r in results:
            if not r["matched"]:
                print("  #%-3d %-56s 期望=%r 实得=%r %s"
                      % (r["index"], r["image_rel"], r["expect"], r["actual"] or "（空）",
                         ("[%s]" % r["error"]) if r["error"] else ""))

    if s["errored"]:
        return EXIT_NOT_ALL_PASSED
    if args.fail_under is not None and (s["accuracy"] or 0.0) < args.fail_under:
        print("[门限未达] 准确率 %.2f%% < --fail-under %.2f%%" % (s["accuracy"], args.fail_under),
              file=sys.stderr)
        return EXIT_ACCURACY_GATE
    if s["passed"] != s["total"]:
        return EXIT_NOT_ALL_PASSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
