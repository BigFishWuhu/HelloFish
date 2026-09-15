FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# The cloud server reuses the SQLite schema and query helpers from agent/.
COPY agent ./agent
COPY cloud ./cloud

RUN useradd --create-home --uid 10001 hellofish \
    && mkdir -p /app/cloud/data \
    && chown -R hellofish:hellofish /app

USER hellofish
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)"

CMD ["python", "cloud/server.py", "--host", "0.0.0.0", "--port", "8787"]
