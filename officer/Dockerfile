FROM python:3.10-slim

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 mcpuser

# Install dependencies
COPY officer/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY common/ /app/common/
COPY officer/ /app/officer/

ENV PYTHONPATH=/app
ENV PORT=9002
ENV ROLE=officer
ENV PYTHONUNBUFFERED=1

USER mcpuser

EXPOSE 9002

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9002/healthz')" || exit 1

CMD ["python", "-m", "uvicorn", "officer.server:app", "--host", "0.0.0.0", "--port", "9002"]
