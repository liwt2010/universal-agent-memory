FROM python:3.12-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy package files
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install the package
RUN pip install --no-cache-dir -e .

# Install optional production dependencies
RUN pip install --no-cache-dir \
    chromadb \
    tiktoken \
    sentence-transformers \
    redis \
    neo4j \
    || true

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3111/health')" || exit 1

# Default environment
ENV UAMS_LOG_LEVEL=INFO
ENV UAMS_STORAGE_BACKEND=sqlite
ENV UAMS_SQLITE_PATH=/data/uams.db
ENV UAMS_HEALTH_PORT=3111

# Create data volume
VOLUME ["/data"]

# Expose health port
EXPOSE 3111
# Default command: start a simple health server with a UAMS instance
# (CMD array uses single-line JSON; multi-line Python lives in a file)
COPY docker_entrypoint.py /app/docker_entrypoint.py
CMD ["python", "/app/docker_entrypoint.py"]
