# Lightweight production image: runs DenseNet121 through ONNX Runtime (no PyTorch),
# so it fits small hosts such as Render's free plan. Build the ONNX file with
# `python -m scripts.export_onnx`. Firebase credentials come from environment
# variables (FIREBASE_SERVICE_ACCOUNT_JSON, FIREBASE_WEB_CONFIG), never from the image.
FROM python:3.10-slim
WORKDIR /app
COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt
COPY . .
RUN useradd -m -u 1000 app && chown -R app /app
USER app
ENV PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/mpl PORT=10000
EXPOSE 10000
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'
