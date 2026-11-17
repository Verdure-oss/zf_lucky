# 与既有 bili 项目同基础镜像，宿主机为 aarch64，python:3.10-slim 有原生 arm64
FROM python:3.10-slim

# 时区必须设置：调度依赖容器本地时间，缺省 UTC 会让 09:00 偏移 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl ca-certificates \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，代码改动可命中缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

COPY . /app

# 数据与日志目录（部署时由 volume 覆盖）
RUN mkdir -p /app/data /app/logs

# 入口先探测 Selenium 就绪，再启动主程序
ENTRYPOINT ["python", "-m", "scripts.entrypoint"]
