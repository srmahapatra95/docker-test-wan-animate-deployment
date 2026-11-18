FROM alpine:3.22

ENV PYTHONUNBUFFERED=1

# start.sh uses Bash; Python and pip run the RunPod handler.
RUN apk add --no-cache bash python3 py3-pip

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY handler.py .
COPY start.sh .

RUN chmod +x /app/start.sh

ENTRYPOINT ["/app/start.sh"]
