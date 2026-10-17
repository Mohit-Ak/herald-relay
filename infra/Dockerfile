# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt

# ---- Final stage ----
FROM python:3.12-slim

WORKDIR /app/backend

COPY --from=builder /install /usr/local
COPY backend/ .

# Cloud Run mounts the env file (Secret Manager: herald-relay-env) at
# /secrets/env; main.py's load_dotenv() reads ./.env.
RUN rm -f .env && ln -sf /secrets/env /app/backend/.env

ENV PYTHONUNBUFFERED=1
EXPOSE 8082

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8082"]
