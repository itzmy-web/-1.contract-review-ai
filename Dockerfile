# 合同审批审查系统 - 生产镜像
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# opencv / onnxruntime（rapidocr 依赖）所需的系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 层缓存，代码变了不重装依赖）
COPY requirements.txt .
RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制代码与静态资源
COPY . .

EXPOSE 8010

# 注意：单 worker。审查任务/规则/审计日志/审批待办均保存在进程内存（并定期落盘到
# data/*.json），多 worker 会导致「同一任务在不同 worker 之间不可见」以及
# 两个进程互相覆盖 JSON 文件。要横向扩容请先换成 SQLite/MySQL 等共享存储。
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
