FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/allen141/thermostat" \
      org.opencontainers.image.description="Local thermostat monitoring dashboard"

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ./
COPY homekit_manager.py ./
COPY static ./static
RUN addgroup -S thermostat && adduser -S -G thermostat -h /app thermostat && mkdir -p /app/data && chown -R thermostat:thermostat /app
USER thermostat
ENV HOST=0.0.0.0 PORT=8787 POLL_SECONDS=300 PYTHONUNBUFFERED=1
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/status', timeout=3)"
CMD ["python3", "server.py"]
