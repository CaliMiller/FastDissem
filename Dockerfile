FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt waitress
COPY fastdissem ./fastdissem
VOLUME /data
EXPOSE 8787
# 0.0.0.0 so the port can be published; set a web password in Settings → Web UI
CMD ["python", "-m", "fastdissem", "run", "--data-dir", "/data", "--host", "0.0.0.0"]
