"""补跑闸门：定时补跑（21:30 / 23:00）若当天已成功产出则跳过，保证一天只成功刷新一次。

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
    fresh = meta.get("bj_date") == today and not meta.get("stale")
    print("false" if fresh else "true")


if __name__ == "__main__":
    main()
