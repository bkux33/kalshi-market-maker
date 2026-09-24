FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY alphalab ./alphalab
RUN pip install --no-cache-dir ".[llm]" && useradd --create-home --uid 10001 lab && mkdir -p /data && chown lab /data
USER lab
VOLUME ["/data"]
EXPOSE 8080
# Default is harmless: print the effective (redacted) config. Services choose their command in compose.
CMD ["alphalab", "config"]
