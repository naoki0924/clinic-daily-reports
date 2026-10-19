# 4院 日報集計（Web画面＋スケジューラー＋Talknote収集用のヘッドレスChromium）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tokyo \
    APP_DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY scripts ./scripts

# データ（SQLite・暗号化した原本・暗号鍵・バックアップ）は /data に置く。Railway ではボリュームを /data に接続する
EXPOSE 8100
CMD ["python", "-m", "app.cli", "serve", "--host", "0.0.0.0", "--port", "8100"]
