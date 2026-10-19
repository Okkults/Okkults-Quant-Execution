# ============ Okkults Quant Execution — Dockerfile ============
# Runs the terminal headless (full_auto) 24/7 — e.g. on a $3/mo VPS in US-East
# so an idle laptop never misses the 3:50 PM entry window or the 9:30 AM exit.

# --- stage 1: wheels ------------------------------------------------------
FROM python:3.11-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# --- stage 2: runtime ----------------------------------------------------
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --from=builder /app/requirements.txt ./
COPY . .
# Non-root runtime user; secrets ride in via -e flags (never baked in)
RUN useradd --create-home okkults && chown -R okkults /app
USER okkults
# HEADLESS=1 tells the terminal to skip interactive bits (wizard still
# available but not auto-triggered; unattended restarts go straight to run)
ENV HEADLESS=1
CMD ["python", "main.py"]
