# Runs verify.py on the compose network. The repository is mounted at /work at run time; nothing is copied in.
FROM python:3.12-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.3" "httpx==0.28.1"
WORKDIR /work
ENV PYTHONUNBUFFERED=1
