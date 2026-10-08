FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt
COPY . /app
ENV PORT=10000
EXPOSE 10000
CMD ["sh", "-c", "exec waitress-serve --listen=0.0.0.0:${PORT:-10000} --threads=4 server.app:app"]
