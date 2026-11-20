FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RIMBENCH_DATA=/var/lib/rimbench HOST=0.0.0.0 PORT=8000
WORKDIR /opt/rimbench
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY rimbench ./rimbench
COPY static ./static
COPY app.py .
RUN useradd -U -u 11460 rimbench && install -d -o rimbench -g rimbench /var/lib/rimbench
USER rimbench
EXPOSE 8000
CMD ["python", "app.py"]
