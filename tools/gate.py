"""补跑闸门：定时补跑（21:30 / 23:00）若今晚已经成功产出则跳过。

判定条件刻意不是「当天生成过」，而是「当天北京时间 20:00 之后生成过」：
白天由代码 push / 手动触发刷出来的快照，价格日仍是上一交易日，
不能占掉晚上 21:00 的正点刷新。当天 20:00 之后的任何成功产出才算今晚已跑。

手动触发、代码 push 一律放行；上一次是「陈旧快照」时也放行，让补跑有机会修好数据。
只输出一行 true / false，供 GitHub Actions 的 if 条件使用。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))


def main():
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    if event != "schedule":
        print("true")
        return
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("prev/meta.json")
    today = dt.datetime.now(TZ).date().isoformat()
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print("true")
        return
    if meta.get("stale"):
        print("true")
        return
    try:
        gen = dt.datetime.fromisoformat(str(meta.get("generated_at") or "")).astimezone(TZ)
        fresh = gen.date().isoformat() == today and gen.hour >= 20
    except Exception:  # noqa: BLE001
        fresh = meta.get("bj_date") == today
    print("false" if fresh else "true")


if __name__ == "__main__":
    main()
