# PTI 服务端镜像：只含 Python + server.py，零第三方依赖。
# 基础镜像 python:3.12-alpine 约 50MB 级别；数据库不进镜像，走 ./data:/data 挂载。
FROM python:3.12-alpine
WORKDIR /app
COPY server.py /app/server.py
RUN mkdir -p /data
EXPOSE 8000
ENV PORT=8000
ENV PTI_DB_PATH=/data/pti.db
CMD ["python3", "server.py"]
