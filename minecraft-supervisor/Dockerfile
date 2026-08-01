FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY supervisor.py /app/supervisor.py

EXPOSE 8791

CMD ["python", "/app/supervisor.py"]
