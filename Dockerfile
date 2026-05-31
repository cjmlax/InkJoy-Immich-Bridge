FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/immich_inkjoy_bridge.py .

CMD ["python", "immich_inkjoy_bridge.py"]
