FROM python:3.11-slim

# Keep the syntax-check toolchains in the same image as the analyzer so users
# do not need to install compilers on their own machines.
RUN apt-get update \
    && apt-get install -y --no-install-recommends default-jdk-headless g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} app:app"]
