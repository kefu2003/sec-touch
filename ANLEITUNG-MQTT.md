# Lüftung in Home Assistant: Mosquitto und Bridge einrichten

Alle Befehle auf dem Ubuntu-Server. Vorher `bus_sniff.py` beenden, denn es darf nur ein Programm an `/dev/ttyUSB1` hängen.

## 1. Mosquitto (MQTT-Broker) installieren

```bash
sudo apt install mosquitto mosquitto-clients python3-paho-mqtt python3-serial
sudo mosquitto_passwd -c /etc/mosquitto/passwd lueftung      # fragt nach einem Passwort
sudo chown root:mosquitto /etc/mosquitto/passwd && sudo chmod 640 /etc/mosquitto/passwd
sudo tee /etc/mosquitto/conf.d/lokal.conf >/dev/null <<'EOF'
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
EOF
sudo systemctl restart mosquitto
```

Test (zweites Terminal): `mosquitto_sub -h localhost -u lueftung -P <passwort> -t '#' -v`

## 2. MQTT in Home Assistant hinzufügen

Einstellungen → Geräte & Dienste → Integration hinzufügen → **MQTT**.
- Broker: `localhost`, wenn der HA-Container mit `network_mode: host` läuft, sonst die IP des Servers
- Port: `1883`, Benutzer: `lueftung` und das Passwort von oben

## 3. Bridge installieren

Die drei Dateien aus den Projektdateien (`sec_touch_mqtt.py`, `sec-touch-mqtt.conf`, `sec-touch-mqtt.service`) auf den Server kopieren, dann:

```bash
sudo mkdir -p /opt/sec-touch
sudo cp sec_touch_mqtt.py /opt/sec-touch/
sudo cp sec-touch-mqtt.conf /etc/ && sudo chmod 600 /etc/sec-touch-mqtt.conf
sudo nano /etc/sec-touch-mqtt.conf          # Passwort eintragen, Räume prüfen
```

Zuerst von Hand testen:

```bash
sudo python3 /opt/sec-touch/sec_touch_mqtt.py --config /etc/sec-touch-mqtt.conf
```

Nach spätestens 10 s sollte „am Bus angemeldet (Adresse 5)“ erscheinen. In HA taucht dann das Gerät **Lüftung SEC-Touch** mit 6 Auswahlfeldern auf (Stufe 1 bis 6, Automatik).

Wenn das klappt, mit Strg+C beenden und als Dienst einrichten:

```bash
sudo cp sec-touch-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sec-touch-mqtt
journalctl -u sec-touch-mqtt -f              # Log ansehen
```

## Hinweise

- `/dev/ttyUSB1` kann sich nach einem Neustart ändern. Falls das passiert, stellen wir auf `/dev/serial/by-id/...` um.
- Bisher angeboten werden nur die am Gerät geprüften Werte: Stufe 1 bis 6 und Automatik. Stoßlüftung, Schlummer usw. kommen dazu, sobald wir die Werte am Touch mitgeschnitten haben.
