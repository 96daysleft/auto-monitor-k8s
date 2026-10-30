FROM python:3.14-slim

# setuptools-scm can't see .git inside the build: the release workflow
# passes the tag version here.
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_FILE=/config/config.yaml

WORKDIR /app

RUN groupadd -g 1001 app && useradd -r -u 1001 -g app app

COPY pyproject.toml README.md LICENSE ./
COPY auto_monitor/ ./auto_monitor/
RUN pip install --no-cache-dir . && rm -rf /app/build

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

CMD ["auto-monitor-k8s"]
