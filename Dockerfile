# --- dashboard build
FROM node:22-slim AS web
WORKDIR /web
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# --- runtime
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres,redis]"
COPY config ./config
COPY --from=web /web/dist ./dashboard/dist
EXPOSE 8000
VOLUME ["/app/data"]
CMD ["imbalance", "live", "--daemon"]
