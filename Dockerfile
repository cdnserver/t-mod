FROM python:3.12-slim

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
