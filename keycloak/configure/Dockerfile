FROM python:3.12-slim
RUN pip install --no-cache-dir requests==2.32.3
WORKDIR /app
COPY configure.py .
CMD ["python", "-u", "configure.py"]
