FROM python:3.13-alpine
WORKDIR /app
COPY server.py .
ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
USER nobody
CMD ["python", "server.py"]
