FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ARENA_DB=/app/data/arena.sqlite3 PORT=8010
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 alphalab \
    && mkdir -p /app/data && chown alphalab:alphalab /app/data
COPY --chown=alphalab:alphalab app ./app
COPY --chown=alphalab:alphalab static ./static
USER alphalab
EXPOSE 8010
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8010} --workers 1 --no-access-log"]
