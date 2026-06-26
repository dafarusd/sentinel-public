# Sentinel Commands

## SSH in
ssh user@192.0.2.10

## Lifecycle
cd ~/sentinel
./sentinel.sh start
./sentinel.sh stop
./sentinel.sh restart
./sentinel.sh status

## Logs (Ctrl+C to exit)
./sentinel.sh logs
./sentinel.sh logs wifi
./sentinel.sh logs bt
./sentinel.sh logs ingest
./sentinel.sh logs sdr_adsb

## Is data flowing?
sqlite3 /mnt/ssd/sentinel-data/sentinel.db "
SELECT
  (SELECT MAX(timestamp) FROM bt_advertisements) as bt,
  (SELECT MAX(timestamp) FROM wifi_frames) as wifi,
  (SELECT MAX(timestamp) FROM sdr_adsb) as adsb;
"

## Disk usage
df -h /mnt/ssd

## Power off
./sentinel.sh stop
sudo shutdown -h now

## Query Pi DB from Framework

### One-time install
sudo apt install sshfs -y
mkdir -p ~/mnt/pi-sentinel-data

### Mount (Pi must be on)
sshfs user@kali-raspberrypi:/mnt/ssd/sentinel-data ~/mnt/pi-sentinel-data

### Query directly
sqlite3 ~/mnt/pi-sentinel-data/sentinel.db ".tables"
sqlite3 ~/mnt/pi-sentinel-data/sentinel.db "SELECT COUNT(*) FROM devices;"

### Use with an LLM coding assistant or opencode
Point it at ~/mnt/pi-sentinel-data/sentinel.db
Example: "query ~/mnt/pi-sentinel-data/sentinel.db for unique BT devices in the last hour"

### Unmount before Pi shutdown
fusermount -u ~/mnt/pi-sentinel-data
