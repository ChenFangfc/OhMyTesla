FROM python:3.13-slim

# ffmpeg 用于视频合成；Noto CJK 字体用于视频叠加层的中文渲染
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ohmytesla

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
