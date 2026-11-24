FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ARG APP_VERSION=1.3.0
ENV APP_VERSION=$APP_VERSION DATA_DIR=/data SOURCE_DIR=/srv PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/gesund')"
# Ein Prozess mit mehreren Threads: die Hintergrundprüfung (Build, Freigabe) läuft so nur einmal.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "8", "--timeout", "900", "app.main:app"]
