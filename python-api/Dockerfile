FROM python:3.12-slim
WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY wrapper.py .
COPY packagemanager.py .
COPY exceptions.py .
COPY worker.py .

RUN mkdir -p /app/scripts /app/venvs

ENV SCRIPTS_DIR=/app/scripts
ENV VENV_BASE=/app/venvs

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
