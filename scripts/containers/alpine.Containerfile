# Alpine 3.24: stock python3 and py3-* apk packages only; no pip, venv or uv.
FROM docker.io/library/alpine:3.24
RUN apk add --no-cache python3 py3-fastapi py3-pydantic py3-jinja2 uvicorn py3-httpx \
    py3-argon2-cffi py3-python-multipart git procps tzdata
