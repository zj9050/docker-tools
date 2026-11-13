FROM python:3.11-alpine

ENV TZ=Asia/Shanghai \
    TZ_OFFSET=8 \
    PORT=10000 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py config.py docker_api.py manager.py auth.py \
     console.py monitor.py resources.py sysinfo.py alerts.py store.py ./
COPY static ./static

RUN mkdir -p /app/data

EXPOSE 10000
CMD ["python", "server.py"]
