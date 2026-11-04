FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src TZ=Europe/Madrid

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev curl tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
CMD ["python", "-m", "fantasy_coconut.ingest.scheduler"]
