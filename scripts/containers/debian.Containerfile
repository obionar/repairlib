# Debian 13 (trixie): stock python3 and python3-* packages only; no pip, venv or uv.
FROM docker.io/library/debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    python3 python3-fastapi python3-jinja2 python3-uvicorn python3-pydantic python3-httpx \
    python3-argon2 python3-python-multipart git ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
