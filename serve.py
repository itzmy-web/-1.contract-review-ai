"""统一启动入口（部署 / 本地通用）。

为什么单独放一个文件，而不是让部署命令写 `--port $PORT`：
部分部署执行器不展开命令里的 shell 变量，会直接报
`Invalid value for '--port': '--workers'`。这里改成由 Python 自己读环境变量，
不再依赖 shell 展开，本地和云端同一条命令。

    python serve.py                # 默认 8010
    PORT=8080 python serve.py      # 跟随平台注入的端口
"""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    port = int(str(os.getenv("PORT") or "8010").strip() or 8010)
    host = (os.getenv("HOST") or "0.0.0.0").strip()
    # 单 worker：任务 / 规则 / 审计日志 / 审批待办都在进程内存并落盘 JSON，
    # 多 worker 会导致任务跨进程不可见、并互相覆盖数据文件（详见 README）。
    uvicorn.run("backend.main:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
