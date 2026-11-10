"""运行垃圾清理 + 体积上限（默认 500MB）自动裁剪。

双保险，保证「运行时间越长占用越大」不会发生：
  1. 结构性：部署走「孤立单提交 + 强推 gh-pages」，旧提交立即变成不可达对象，
     仓库任何时刻只保留一份产物，体积天然恒定；
  2. 数值性：本脚本在发布前对 dist/ 做体检，超限时按优先级逐级裁剪，
     并清理 __pycache__ / *.pyc / *.log / *.tmp / 缓存目录等运行垃圾。
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIST = BASE / "dist"

JUNK_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".cache", ".ipynb_checkpoints"}
JUNK_GLOBS = ["*.pyc", "*.pyo", "*.log", "*.tmp", "*.temp", "*.sqlite", "*.sqlite-journal",
              "*.whl.part", "*.bak", "*~"]
# 体积吃紧时按这个顺序逐级丢弃（越靠前越先丢）
TRIM_ORDER = [
    "history.json",          # 历史序列缓存：丢了下次自动重抓
    "source_status.json",    # 诊断信息：可重建
    "llms.txt",              # 文本摘要：可重建
    "seed.json",             # 兜底快照：可退化到 main 分支初始 seed
]


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def sweep_junk(root: Path) -> list:
    removed = []
    for d in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not d.is_dir() or d.name not in JUNK_DIRS:
            continue
        # 只清理项目目录内的运行垃圾，不触碰 .git / dist 数据文件
        if ".git" in d.parts:
            continue
        try:
            n = size_of(d)
            shutil.rmtree(d)
            removed.append({"path": str(d.relative_to(BASE)), "bytes": n})
        except OSError:
            pass
    for pat in JUNK_GLOBS:
        for f in root.rglob(pat):
            if not f.is_file() or ".git" in f.parts:
                continue
            try:
                n = f.stat().st_size
                f.unlink()
                removed.append({"path": str(f.relative_to(BASE)), "bytes": n})
            except OSError:
                pass
    return removed


def trim_history(path: Path, keep: int) -> bool:
    """把 history.json 每条序列截到 keep 个点，仍超限则继续缩短。"""
    if not path.exists():
        return False
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    changed = False
    for rec in (obj.get("series") or {}).values():
        for kind in ("nav", "price"):
            arr = rec.get(kind) or []
            if len(arr) > keep:
                rec[kind] = arr[-keep:]
                changed = True
    for k, arr in (obj.get("bench") or {}).items():
        if len(arr) > keep:
            obj["bench"][k] = arr[-keep:]
            changed = True
    obj["premium_history"] = {}
    if changed or keep < 320:
        path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-mb", type=int, default=500)
    ap.add_argument("--no-sweep", action="store_true")
    args = ap.parse_args()
    limit = args.limit_mb * 1024 * 1024

    report = {"limit_mb": args.limit_mb, "limit_bytes": limit,
              "before_bytes": size_of(DIST), "actions": [], "after_bytes": 0,
              "ok": True}

    if not args.no_sweep:
        swept = sweep_junk(BASE)
        report["actions"].append({"action": "sweep_junk", "count": len(swept),
                                 "bytes": sum(x["bytes"] for x in swept),
                                 "sample": swept[:8]})

    cur = size_of(DIST)
    if cur > limit:
        # 先缩短历史序列（保留 180 → 90 → 60 个交易日），再按优先级删文件
        for keep in (180, 90, 60):
            if cur <= limit:
                break
            trim_history(DIST / "history.json", keep)
            cur = size_of(DIST)
            report["actions"].append({"action": f"trim_history_keep_{keep}",
                                      "bytes": size_of(DIST)})
        for name in TRIM_ORDER:
            if cur <= limit:
                break
            f = DIST / name
            if f.exists():
                n = f.stat().st_size
                f.unlink()
                cur -= n
                report["actions"].append({"action": f"drop_{name}", "bytes": n})

    report["after_bytes"] = size_of(DIST)
    report["ok"] = report["after_bytes"] <= limit
    report["files"] = sorted(
        [{"name": p.name, "bytes": p.stat().st_size} for p in DIST.iterdir() if p.is_file()],
        key=lambda x: -x["bytes"])[:20]
    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "cleanup_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "files"},
                     ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(f"dist 仍超过 {args.limit_mb}MB，拒绝发布")
    return report


if __name__ == "__main__":
    main()
