FROM python:3.12-slim

WORKDIR /app
COPY . /app

CMD ["python", "src/radar_loop.py", "--iterations", "1000000", "--interval", "60"]
