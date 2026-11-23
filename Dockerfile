FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NAVIBLEND_DATA=/config \
    PORT=4545 \
    TZ=Etc/UTC

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY naviblend ./naviblend

RUN mkdir -p /config && chmod 777 /config
VOLUME /config
EXPOSE 4545

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"4545\")}/health', timeout=4)"

CMD ["python", "-m", "naviblend"]
