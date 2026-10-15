
# Detection settings (change these for the experiment)
POWER_LIMIT = 8          # W
TIME_LIMIT = 10          # minutes
MAX_GAP = 150            # seconds between measurements

# InfluxDB settings on Raspberry Pi
INFLUX_URL = "http://localhost:8086"
ORG = "CHANGE_ME"
BUCKET = "CHANGE_ME"
POLL_SECONDS = 30

# Optional: add a visible event marker to an existing Grafana dashboard
GRAFANA_URL = "http://localhost:3000"
GRAFANA_DASHBOARD_UID = ""   # e.g., "abCd123" from the dashboard URL
