FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements-server.txt ./
RUN pip install --no-cache-dir --no-deps -r requirements-server.txt \
    && pip check \
    && mkdir /state \
    && chown 10001:10001 /state
COPY server.py capsule.py capsule_manifest.py capsule_pipeline.py network.py ./
COPY web/ ./web/
COPY docs/openapi.json ./docs/
USER 10001:10001
EXPOSE 8787
CMD ["python", "server.py"]
