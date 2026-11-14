FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY roadwatch ./roadwatch
RUN pip install --no-cache-dir .
ENV ROADWATCH_DB=/data/roadwatch.sqlite3 PORT=8000
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "uvicorn roadwatch.server:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'"]
