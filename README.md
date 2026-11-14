# LowcostSlamServer
 
This project needs a Python SDK for your camera to use.

If you're using Insta360 cameras, you can use the [Insta360 Camera SDK Python Wrapper](https://github.com/gureckyfrantisek/Insta360CameraSDKPythonWrapper) I made.

## Setup
Put the wrapped SDK into the sdk folder.

You need the following installed:

- Python 3

### Run the venv
```bash
python3 -m venv .venv
```

Then activate it using
```bash
source .venv/bin/activate
```

### Install dependencies
```bash
pip install -r requirements.txt
```

### Run the server
The IMU (LIS3DH accelerometer) and the GNSS receiver (ZED-F9P) are read over I2C, each on its own bus of the
40-pin header: the GNSS receiver on pins 3/5 and the IMU on pins 27/28. Buses and addresses are in
`app/core/config.py` and can be overridden in `.env` (`IMU_I2C_BUS`, `GNSS_I2C_BUS`, `IMU_I2C_ADDRESS`,
`GNSS_I2C_ADDRESS`, `IMU_I2C_HZ`). The default bus names `/dev/i2c-header-3-5` and `/dev/i2c-header-27-28` come
from a udev rule, because the plain `/dev/i2c-N` numbers change between boots.

On the Radxa, `sudo sh scripts/bench/setup_i2c_gpio.sh` installs the tools, loads `i2c-dev` at boot, creates those
names and adds your user to the `i2c` and `gpio` groups (log in again afterwards). The sensors then need no sudo.
Check the wiring with `/usr/sbin/i2cdetect -y -r <N>`: the F9P answers at `0x42`, the LIS3DH at `0x18`.

The IMU data is stored raw (12 bit counts, 1 mg per count at +-2 g), without calibration or correction.

Every IMU sample gets a time from the kernel time stamp of the sensor's FIFO interrupt (header pin 29), and the
PPS of the GNSS receiver (pin 7) is logged on the same clock, so the samples can be mapped to GPS time afterwards.
While recording, the CPUs are kept out of the deep sleep states (needs the `gpio` group and the udev rule of the
setup script). Without the GPIO edges the server still works, with times good to about a sample period. A
recording writes `imu.csv`, `pps.csv`, `gnss.ubx`, `gnss_index.csv` and `imu_info.json`; see [docs/timing.md](docs/timing.md)
for the formats, the measured accuracy and the data volume (16 MB per hour at 100 Hz).

The server needs sudo permissions for USB port access (the camera).
```bash
sudo .venv/bin/uvicorn app.main:app --reload
```

### Install as a service (autostart at boot)
On the Radxa, from a checkout of the repository:
```bash
sudo sh deploy/install.sh
```
It installs the packages, copies the server to `/opt/LowcostSlamServer` with its own virtual environment, installs the
udev rules (names of the header I2C buses, GPIO and PM QoS access, USB stick automount) and the systemd unit, then
enables and starts it. The server comes up at every boot and is restarted 3 seconds after it dies. Run the script
again to update the installed copy, `/opt/LowcostSlamServer/.env` is kept. Settings: `deploy/env.example`.

```bash
systemctl status lowcost-slam-api       # is it running
journalctl -u lowcost-slam-api -f       # the log (kept on disk across reboots)
sudo systemctl restart lowcost-slam-api
sudo systemctl disable --now lowcost-slam-api   # back to running it by hand
```
The `radxa` user is in the `sudo` group, so `sudo` asks for its password. The service runs as root (the camera
SDK needs USB access) and the sensors need no root: with the udev rules, the `gpio` and `i2c` groups are enough.

### USB stick
Recordings go to a USB stick when one is mounted (in the `LowcostSlam` folder of the stick), else to
`~/measurements` (`LOCAL_DATA_PATH`). Install the udev rule once and any stick (FAT, exFAT, NTFS, ext4) is mounted under
`/media/usb/<name>` when it is plugged in, and unmounted by systemd when it is pulled out:
```bash
sudo cp deploy/99-usb-automount.rules /etc/udev/rules.d/ && sudo udevadm control --reload
```
One hour of IMU and GNSS logging takes about 23 MB at the default rates (100 Hz IMU, 1 Hz GNSS; the video stays on the
camera card). `/status` and `/start` report the room left and warn when less than 3 hours of recording fit; `/start` is
refused when less than 10 minutes fit. `/projects` lists the projects of the stick and of the local disk, and
`/download` copies a local project to the stick (a project recorded on the stick is already there).

### Tests
The sensors are simulated, so no hardware is needed for the sensor tests:
```bash
pip install pytest httpx2
python -m pytest tests
```
`scripts/sync_test.sh` copies the working tree to a scratch folder on the Radxa and runs the tests there.

### Try it out
Open the generated docs in [http://127.0.0.1:8000/docs#](http://127.0.0.1:8000/docs#) and try hitting the endpoints there.