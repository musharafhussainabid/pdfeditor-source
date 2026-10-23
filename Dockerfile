# Runs locally (docker run -p 8080:8080), on a VPS (deploy/vps), Google Cloud Run,
# Cloudflare Containers and AWS Lambda (via the Lambda Web Adapter, which is inactive everywhere else).
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter
# Look-alike fonts so new text matches Calibri, Cambria, Arial, Times New Roman, Courier New
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation fonts-dejavu-core fonts-noto-core \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY edit_doc.py app.py index.html ./
# Run as an unprivileged user, not root
RUN useradd --system --uid 10001 --no-create-home app
USER app
ENV PYTHONUNBUFFERED=1 PORT=8080 AWS_LWA_PORT=8080 AWS_LWA_READINESS_CHECK_PATH=/healthz
EXPOSE 8080
# app.py logs every request as JSON, so uvicorn's own access log is off.
# WORKERS: parallel processes (1 on Lambda/Cloud Run; about cores - 2 on a VPS).
# FORWARDED_ALLOW_IPS: trust the proxy in front (Caddy) to pass on the user's real IP.
ENV WORKERS=1 FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 8080 --no-access-log --workers \"$WORKERS\" --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" --timeout-keep-alive 30"]
