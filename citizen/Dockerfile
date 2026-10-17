FROM python:3.10-slim

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 mcpuser

# Install dependencies
COPY citizen/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY common/ /app/common/
COPY citizen/ /app/citizen/

ENV PYTHONPATH=/app
ENV PORT=9001
ENV ROLE=citizen
ENV PYTHONUNBUFFERED=1

USER mcpuser

EXPOSE 9001

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9001/healthz')" || exit 1

CMD ["python", "-m", "uvicorn", "citizen.server:app", "--host", "0.0.0.0", "--port", "9001"]
