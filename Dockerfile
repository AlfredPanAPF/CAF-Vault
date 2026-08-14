# CAF-Vault image — serves both compose services:
#   caf-vault:        graph serve --host 0.0.0.0 --port 8600   (default CMD)
#   caf-vault-worker: graph loop                               (command override)
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

EXPOSE 8600
CMD ["graph", "serve", "--host", "0.0.0.0", "--port", "8600"]
