# server.py uses the cgi module, which Python 3.13 removed — stay on 3.12.
FROM python:3.12-slim

WORKDIR /app

# Non-root runtime user plus a writable database directory.
RUN useradd --system --uid 10001 --create-home site \
    && mkdir -p /data \
    && chown -R site:site /data

COPY server.py index.html login.html ./

USER site

ENV IMAGE_SITE_HOST=0.0.0.0 \
    IMAGE_SITE_PORT=4180 \
    IMAGE_SITE_DB=/data/users.db \
    IMAGE_SITE_UPSTREAM=http://m365-copilot2api:4141

EXPOSE 4180
VOLUME ["/data"]

CMD ["python3", "-u", "server.py"]
