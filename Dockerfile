# -------------------------
# BUILD STAGE
# -------------------------
FROM python:3.14-slim AS builder

WORKDIR /app

# Install dependencies into a self-contained venv we can copy across
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# -------------------------
# FINAL IMAGE
# -------------------------
FROM python:3.14-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 carepoint

WORKDIR /app

# Copy from builder stage
COPY --from=builder /opt/venv /opt/venv
COPY --chown=carepoint:carepoint . .

COPY entrypoint.sh /usr/bin/
RUN chmod +x /usr/bin/entrypoint.sh

USER carepoint

EXPOSE 8000

ENTRYPOINT ["entrypoint.sh"]
# Behind Caddy: trust X-Forwarded-* so redirects and audit IPs are the client's.
# Only reachable on the internal 'web' network, so trusting every hop is safe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
