
# Device Left-On Alert

This is a use case for our Pi Energy Monitor project.

## What it does
The program checks if an electrical device stays ON
for longer than a specified time.

If the power and time limits are reached,
the program generates an alert.

## Technologies
- Python
- Raspberry Pi
- Shelly Plug S Gen3
- InfluxDB
- Grafana

## Testing
We tested the detection logic using real power
readings collected from the Shelly plug.

All 17 local tests passed.

Live integration with InfluxDB and Grafana
still needs to be tested on the Raspberry Pi.
