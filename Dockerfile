# The image serves a prebuilt knowledge base (data/kb.sqlite); scraping runs offline, not in the container.
FROM python:3.11-slim

# Run as a non-root user. The host sets PORT (Render uses 10000); 7860 is the local default.
RUN useradd -m -u 1000 app
WORKDIR /home/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app app ./app
COPY --chown=app data/kb.sqlite ./data/kb.sqlite

USER app
ENV PORT=7860 PYTHONUNBUFFERED=1
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health', timeout=4)"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
