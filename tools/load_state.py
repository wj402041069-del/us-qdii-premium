"""从 gh-pages 分支回读上一次成功产物（state / registry / seed / meta）。

GitHub Actions runner 每次都是全新机器，所以历史序列、名单台账、兜底快照
都必须从部署分支读回来。分支不存在时（首次运行）自动退回仓库内 bootstrap 文件。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PAIRS = [
    ("history.json", BASE / "data" / "state.json"),
    ("registry.json", BASE / "data" / "registry.json"),
    ("seed.json", BASE / "data" / "seed.json"),
    ("meta.json", BASE / "prev" / "meta.json"),
]


def git(*args: str):
    return subprocess.run(["git", *args], cwd=BASE, capture_output=True, text=True)


def main():
    (BASE / "prev").mkdir(exist_ok=True)
    r = git("fetch", "--no-tags", "--depth=1", "origin",
            "+refs/heads/gh-pages:refs/remotes/origin/gh-pages")
    if r.returncode != 0:
        print("gh-pages 尚不存在或不可拉取，使用仓库内 bootstrap 状态：", r.stderr.strip()[:160])
        return
    for remote, local in PAIRS:
        out = git("show", f"origin/gh-pages:{remote}")
        if out.returncode == 0 and out.stdout.strip():
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(out.stdout, encoding="utf-8")
            print(f"回读 {remote} -> {local.relative_to(BASE)} ({len(out.stdout)} bytes)")
        else:
            print(f"{remote} 不存在，沿用 bootstrap：{local.relative_to(BASE)}")


if __name__ == "__main__":
    main()
