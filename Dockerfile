FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/data/pushups.sqlite3

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py tracker_core.py storage.py vision.py video_workflow.py rich_messages.py ./
COPY rules.txt prompt.txt ./
RUN mkdir -p /app/data
CMD ["python", "bot.py"]
