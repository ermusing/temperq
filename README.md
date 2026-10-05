# temperq

A small daemon for a Raspberry Pi Zero W that connects to Home Assistant over MQTT, using
MQTT Discovery. It brings in:

- **an ElectraSmart AC**, through Electra's cloud and the
  [`electrasmart`](https://pypi.org/project/electrasmart/) library. It appears as a climate
  entity, where you can set the mode, fan speed and target temperature.
- **an AHT30 temperature and humidity sensor** on I2C. It appears as two sensors, and it
  also supplies the AC's current temperature.

## Install on the Pi

Use Raspberry Pi OS Lite, 32-bit (Trixie, or Bookworm). Wire the AHT30 to the Pi's I2C pins:
SDA to pin 3, SCL to pin 5, VIN to 3.3 V on pin 1, and GND to pin 6. Then copy this
repository to the Pi and run:

```sh
sudo deploy/install.sh
```

The script creates a `temperq` system user, a venv in `/opt/temperq/.venv`, the config
files in `/etc/temperq/` and a systemd unit, and it turns on I2C. It ends by printing the remaining steps:

1. Set the MQTT host in `/etc/temperq/config.yaml`.
2. Set the MQTT username and password in `/etc/temperq/credentials.yaml`.
3. Run `temperq login` to sign in to ElectraSmart. It sends an SMS code to the phone
   registered in the ElectraSmart app.
4. Run `temperq read-sensor` to check the sensor, then start the service.

The settings are explained in [deploy/config.example.yaml](deploy/config.example.yaml).

## Develop

Development works on Windows, using the fake sensor:

```sh
python -m venv .venv
.venv/Scripts/activate
pip install -e ".[dev]"
pytest
TEMPERQ_SENSOR_DRIVER=fake temperq read-sensor
```
