FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1

# ffprobe：musicdl_fetch.py 用它校验下载文件的真实时长（60 秒试听截断检测）
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py auto_backfill.py musicdl_fetch.py recommend.py taste.py charts.py enrich_plan.py ./

EXPOSE 8300
# 单工作者即可（下载串行），不启用多进程
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8300"]
