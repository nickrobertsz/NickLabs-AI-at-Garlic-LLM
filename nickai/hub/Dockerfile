FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 NLAG_DATA=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY nlag_hub ./nlag_hub
COPY assets ./assets
COPY dist ./dist
RUN useradd -m -u 1000 nlag && mkdir -p /data /import && chown nlag /data
USER nlag
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)"
CMD ["python", "-m", "nlag_hub", "--port", "8080"]
