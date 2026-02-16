FROM python:3.12-slim

RUN groupadd -r vitalforge && useradd -r -g vitalforge -d /app vitalforge

WORKDIR /app

COPY vitalforge-dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /app/shared/
COPY vitalforge-dashboard/ /app/vitalforge-dashboard/

RUN mkdir -p /app/data && chown -R vitalforge:vitalforge /app

# Entrypoint fixes volume permissions then drops to non-root
RUN printf '#!/bin/sh\nchown -R vitalforge:vitalforge /app/data\nexec su -s /bin/sh vitalforge -c "uvicorn vitalforge-dashboard.app:app --host 0.0.0.0 --port 8086"\n' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8086/health')" || exit 1

CMD ["/app/entrypoint.sh"]
