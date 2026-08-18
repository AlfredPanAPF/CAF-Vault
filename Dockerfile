# CAF-Vault image — serves both compose services:
#   caf-vault:        graph serve --host 0.0.0.0 --port 8600   (default CMD)
#   caf-vault-worker: graph loop                               (command override)
#
# Two stages: node builds the SPA, python runs the app. The built bundle lands
# at /app/frontend/dist, where webapp.py's REPO/frontend/dist lookup finds it.
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# tsc -b && vite build (Filter's build script)
RUN npm run build

FROM python:3.12-slim

# The worker's stage/error logging is plain print(); without this, stdout is
# block-buffered under Docker (no tty) and `docker logs` stays empty for days.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# config.REPO is the parent of the graph/ package directory, so schema/ and
# spike/corpus/ref/ must live next to it. The editable install keeps the
# package rooted at /app (instead of site-packages), which keeps those
# repo-relative paths working in-image.
COPY pyproject.toml ./
COPY graph/ graph/
COPY schema/ schema/
# ref data is copied sparsely — the rest of spike/ (corpus, outputs, GLEIF
# files) stays out of the image; gleif.sqlite is fetched at runtime into /data.
COPY spike/corpus/ref/watchlist.json \
     spike/corpus/ref/company_tickers.json \
     spike/corpus/ref/aliases.json \
     spike/corpus/ref/

RUN pip install --no-cache-dir -e ".[asr]"

# yt-dlp needs a JavaScript runtime for YouTube's player challenges (build
# spec v4 §6.1). The node binary from the build stage is the whole runtime:
# it is statically linked against everything but libstdc++/libgcc, which
# python:3.12-slim already ships, so no apt install and no node_modules.
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node

COPY --from=frontend /build/dist/ ./frontend/dist/

EXPOSE 8600
CMD ["graph", "serve", "--host", "0.0.0.0", "--port", "8600"]
