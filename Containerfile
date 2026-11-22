# RepairLib in a rootless Podman container: Debian 13 packages and this checkout's source.
# The workspace lives on a host directory mounted at /data. Guide: docs/deployment.md
#   podman build -t localhost/repairlib .
FROM docker.io/library/debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    python3 python3-fastapi python3-jinja2 python3-uvicorn python3-pydantic python3-httpx \
    python3-argon2 python3-python-multipart tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY bin /app/bin
COPY src /app/src
ENV REPAIRLIB_WORKSPACE=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
VOLUME /data
WORKDIR /app
# The app listens on 127.0.0.1 only: run with --network host (see the guide).
ENTRYPOINT ["/usr/bin/python3", "/app/bin/repairlib"]
CMD ["service", "foreground", "--workspace", "/data", "--port", "8000"]
