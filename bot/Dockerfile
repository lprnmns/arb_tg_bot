FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y build-essential git && rm -rf /var/lib/apt/lists/*
COPY bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ ./bot
ENV PYTHONPATH=/app
CMD ["python", "-m", "bot.runner"]
