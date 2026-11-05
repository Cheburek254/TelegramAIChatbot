FROM node:22-bookworm-slim AS itmo
WORKDIR /opt/itmo
COPY assets/itmo-package.json ./package.json
COPY assets/itmo-package-lock.json ./package-lock.json
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && apt-get update && apt-get install -y --no-install-recommends libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home student \
    && install -d -m 700 -o student -g student /run/itmo
COPY --from=itmo /usr/local/bin/node /usr/local/bin/node
COPY --from=itmo /opt/itmo /opt/itmo
COPY --chown=student:student app ./app
COPY --chown=student:student scripts/__init__.py scripts/itmo_connect.py ./scripts/
COPY --chown=student:student assets ./assets
USER student
CMD ["python", "-m", "app"]
