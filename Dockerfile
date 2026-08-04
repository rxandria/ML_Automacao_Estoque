# Use official Python 3.10 slim image
FROM python:3.10-slim

# Prevent Python from writing bytecode and ensure logs are unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

# Set working directory
WORKDIR /app

# Install essential system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt-lists/*

# Copy dependencies list and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port (Google Cloud Run automatically sets PORT at runtime)
EXPOSE 10000

# Start server application
CMD ["python", "main.py", "--server-only"]
