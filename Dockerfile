FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements-cloud.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-cloud.txt
COPY --chown=10001:10001 src ./src
USER 10001:10001
ENTRYPOINT ["python", "-m", "hotel_etl", "sync"]
CMD ["--help"]
