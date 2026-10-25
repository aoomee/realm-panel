FROM python:3.12-slim

WORKDIR /panel

# System deps
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    curl wget tar jq bc netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-create dirs realm scripts expect
RUN mkdir -p /etc/realm/rules /etc/realm/health /etc/realm/panel

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "30", "wsgi:application"]
