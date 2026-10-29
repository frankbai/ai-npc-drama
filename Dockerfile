# 零依赖，只需要 Python。
#   docker build -t ai-npc-drama .
#   docker run -p 8800:8800 -v "$PWD/local-data:/data" ai-npc-drama              # 模拟模式
#   docker run -p 8800:8800 -v "$PWD/local-data:/data" --env-file .env ai-npc-drama
FROM python:3.12-slim

WORKDIR /app
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PORT=8800 \
    HOST=0.0.0.0 \
    DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8800

CMD ["python", "run.py"]
