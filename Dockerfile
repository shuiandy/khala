# Khala server image. Data (record repository, state database, TOTP key) lives in the /var/lib/khala volume; a new
# volume gets its key on first start. Settings come from the environment; see compose.yaml and the README.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /var/lib/khala --shell /usr/sbin/nologin khala \
    && install -d -o khala -g khala -m 700 /var/lib/khala

WORKDIR /opt/khala
COPY pyproject.toml README.md LICENSE ./
COPY khala ./khala
RUN pip install --no-cache-dir . && rm -rf /opt/khala/khala

USER khala
WORKDIR /var/lib/khala
VOLUME /var/lib/khala
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4)"
CMD ["khala", "serve", "--host", "0.0.0.0", "--port", "8100"]
