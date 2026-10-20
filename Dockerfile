FROM python:3.12-slim
WORKDIR /app
COPY src ./src
COPY web ./web
COPY mcp ./mcp
COPY server.py run.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/state', timeout=2)"
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]
