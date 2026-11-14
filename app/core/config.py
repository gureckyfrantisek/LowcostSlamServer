# I2C settings, paths,...
import os
from dotenv import load_dotenv
from getpass import getuser


load_dotenv()

# Config variables
LOCAL_DATA_PATH = LOCAL_DATA_PATH = os.path.expanduser("~/measurements") if not os.getenv('LOCAL_DATA_PATH') else os.getenv('LOCAL_DATA_PATH')
BASE_USB_PATH = f"/media/{getuser()}" if not os.getenv('BASE_USB_PATH') else os.getenv('BASE_USB_PATH')

# I2C settings
def _i2c_bus(value):
    """A bus number (2) or a device path (/dev/i2c-header-3-5)"""
    return int(value) if value.isdigit() else value

# The udev rule of scripts/bench/setup_i2c_gpio.sh names the header buses, the plain numbers change between boots.
# The IMU and the GNSS receiver are on separate buses (a shared one makes the GNSS reads delay the IMU reads).
IMU_I2C_BUS = _i2c_bus(os.getenv('IMU_I2C_BUS') or "/dev/i2c-header-27-28")  # header pins 27 and 28 on the Rock Pi X
GNSS_I2C_BUS = _i2c_bus(os.getenv('GNSS_I2C_BUS') or "/dev/i2c-header-3-5")  # header pins 3 and 5
GNSS_I2C_ADDRESS = 0x42 if not os.getenv('GNSS_I2C_ADDRESS') else int(os.getenv('GNSS_I2C_ADDRESS'), 0)
GNSS_I2C_POLL_S = 0.01 if not os.getenv('GNSS_I2C_POLL_S') else float(os.getenv('GNSS_I2C_POLL_S'))  # wait when the receiver has nothing
IMU_I2C_ADDRESS = 0x18 if not os.getenv('IMU_I2C_ADDRESS') else int(os.getenv('IMU_I2C_ADDRESS'), 0)  # LIS3DH: 0x18, or 0x19 with SDO at 3.3 V
IMU_I2C_HZ = 100 if not os.getenv('IMU_I2C_HZ') else int(os.getenv('IMU_I2C_HZ'))  # sensor output data rate
