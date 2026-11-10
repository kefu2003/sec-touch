# Ventilation in Home Assistant: setting up Mosquitto and the bridge

All commands run on the Ubuntu server. Stop `bus_sniff.py` first, because only one program may hold `/dev/ttyUSB1`.

## 1. Install Mosquitto (MQTT broker)

```bash
sudo apt install mosquitto mosquitto-clients python3-paho-mqtt python3-serial
sudo mosquitto_passwd -c /etc/mosquitto/passwd lueftung      # asks for a password
sudo chown root:mosquitto /etc/mosquitto/passwd && sudo chmod 640 /etc/mosquitto/passwd
sudo tee /etc/mosquitto/conf.d/local.conf >/dev/null <<'EOF'
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
EOF
sudo systemctl restart mosquitto
```

Test (second terminal): `mosquitto_sub -h localhost -u lueftung -P <password> -t '#' -v`

## 2. Add MQTT to Home Assistant

Settings → Devices & services → Add integration → **MQTT**.
- Broker: `localhost` if the HA container runs with `network_mode: host`, otherwise the server's IP
- Port: `1883`, user: `lueftung` and the password from above

## 3. Install the bridge

Copy the three project files (`sec_touch_mqtt.py`, `sec-touch-mqtt.conf`, `sec-touch-mqtt.service`) to the server, then:

```bash
sudo mkdir -p /opt/sec-touch
sudo cp sec_touch_mqtt.py /opt/sec-touch/
sudo cp sec-touch-mqtt.conf /etc/ && sudo chmod 600 /etc/sec-touch-mqtt.conf
sudo nano /etc/sec-touch-mqtt.conf          # enter the password, check the rooms
```

Test it by hand first:

```bash
sudo python3 /opt/sec-touch/sec_touch_mqtt.py --config /etc/sec-touch-mqtt.conf
```

Within about 10 s you should see "joined the bus (address 5)". HA then shows the device **Ventilation SEC-Touch** with 6 select entities (Level 1 to 6, Auto).

If that works, stop it with Ctrl+C and install it as a service:

```bash
sudo cp sec-touch-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sec-touch-mqtt
journalctl -u sec-touch-mqtt -f              # follow the log
```

## Notes

- `/dev/ttyUSB1` can change after a reboot. Use a fixed name via a udev rule (e.g. `/dev/rs485-bus`) or `/dev/serial/by-id/...`.
- Only the values verified on the device are offered so far: Level 1 to 6 and Auto. Boost, sleep mode etc. will be added once their values have been captured from the touch panel.
