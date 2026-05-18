FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Bootstrap config.json from config.example.json if not present,
# then start the server. The volume mount in docker-compose means
# config.json will be persisted back to the host automatically.
RUN echo '#!/bin/sh\n\
if [ ! -f /app/config.json ] && [ -f /app/config.example.json ]; then\n\
    echo "config.json not found - copying from config.example.json..."\n\
    cp /app/config.example.json /app/config.json\n\
    echo "NOTE: Default API keys are pre-configured. Edit config.json to customise."\n\
fi\n\
exec python run_server.py' > /app/docker-entrypoint.sh && \
    chmod +x /app/docker-entrypoint.sh

# Expose the port the app runs on
EXPOSE 5555

# Run the server via entrypoint
CMD ["/app/docker-entrypoint.sh"]
