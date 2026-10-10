# 使用官方 Python 3.11-slim 镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装 gosu 用于 Docker 启动入口处的挂载卷权限修复与安全降权
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

# 复制依赖说明文件并安装 Python 包
COPY requirements.txt /app/
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip install --no-cache-dir -r requirements.txt

# 创建专用非 root 运行用户
RUN addgroup --system app && adduser --system --ingroup app app

# 复制项目所有代码并设置基础读取权限兜底
COPY --chown=app:app . /app/
RUN chmod +x /app/docker-entrypoint.sh && \
    chmod -R a+rX /app

# 开放 8051 端口
EXPOSE 8051

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "--workers", "2", "--threads", "4", "--worker-class", "gthread", "--timeout", "120", "--keep-alive", "5", "--bind", "0.0.0.0:8051", "app:app"]
