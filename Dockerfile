FROM postgres:17-bookworm AS postgres-tools

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-core \
        ffmpeg \
        libffi-dev \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-rus \
        postgresql-client \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-liberation2 \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*

# Debian Bookworm ships the PostgreSQL 15 client. T-Mod stores its production
# database on PostgreSQL 17, and pg_dump refuses cross-major server backups.
# Reuse the official PostgreSQL 17 binaries while keeping Debian's libpq stack.
COPY --from=postgres-tools /usr/lib/postgresql/17/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=postgres-tools /usr/lib/postgresql/17/bin/pg_restore /usr/local/bin/pg_restore
COPY --from=postgres-tools /usr/lib/x86_64-linux-gnu/libpq.so.5* /usr/local/lib/
RUN ldconfig \
    && pg_dump --version | grep -F "PostgreSQL) 17." \
    && pg_restore --version | grep -F "PostgreSQL) 17."

RUN groupadd --system --gid 10001 tmod \
    && useradd --system --uid 10001 --gid tmod --home-dir /tmp/tmod tmod \
    && mkdir -p /tmp/tmod /app/data /app/persistent \
    && chown -R tmod:tmod /tmp/tmod /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=tmod:tmod . .

ENV HOME=/tmp/tmod

USER tmod

EXPOSE 8787

CMD ["python", "main.py"]
