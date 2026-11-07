# One image for all shop services; each Deployment picks its service with `command`.
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./
USER 10001
EXPOSE 8080
CMD ["uvicorn", "shop.frontend:app", "--host", "0.0.0.0", "--port", "8080"]
