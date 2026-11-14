# I2C settings, paths,...
import os
from dotenv import load_dotenv
from getpass import getuser


load_dotenv()

# Config variables
LOCAL_DATA_PATH = LOCAL_DATA_PATH = os.path.expanduser("~/measurements") if not os.getenv('LOCAL_DATA_PATH') else os.getenv('LOCAL_DATA_PATH')
BASE_USB_PATH = os.getenv('BASE_USB_PATH') or "/media/usb"  # where the udev rule of deploy/ mounts the sticks (sticks mounted elsewhere are found by their USB device)

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
IMU_RANGE_G = 2 if not os.getenv('IMU_RANGE_G') else int(os.getenv('IMU_RANGE_G'))  # +-2, 4, 8 or 16 g: 1, 2, 4 or 12 mg per count
IMU_FIFO_THRESHOLD = 8 if not os.getenv('IMU_FIFO_THRESHOLD') else int(os.getenv('IMU_FIFO_THRESHOLD'))  # watermark of the 32 sample FIFO, see docs/timing.md

# Time stamps of the sensor edges: the kernel stamps the GPIO edges in its interrupt handler.
# The PPS of the GNSS receiver and the watermark interrupt (INT1) of the IMU are wired to the header.
GPIO_CHIP_LABEL = os.getenv('GPIO_CHIP_LABEL') or "INT33FF:02"  # the chip of the header pins; /dev/gpiochipN changes between boots
GPIO_CHIP = os.getenv('GPIO_CHIP')  # set to force a device path instead of looking the label up
GPIO_LINE_PPS = 21 if not os.getenv('GPIO_LINE_PPS') else int(os.getenv('GPIO_LINE_PPS'))  # header pin 7
GPIO_LINE_IMU_INT = 24 if not os.getenv('GPIO_LINE_IMU_INT') else int(os.getenv('GPIO_LINE_IMU_INT'))  # header pin 29
PM_QOS_DEVICE = os.getenv('PM_QOS_DEVICE') or "/dev/cpu_dma_latency"
PM_QOS_LATENCY_US = 10 if not os.getenv('PM_QOS_LATENCY_US') else int(os.getenv('PM_QOS_LATENCY_US'))  # keeps the CPUs out of C6/C7 while recording

GNSS_RATE_HZ = 1 if not os.getenv('GNSS_RATE_HZ') else int(os.getenv('GNSS_RATE_HZ'))  # measurement rate of the receiver

# Development only: publish a fake position so the BLE position flow can be tested without a fix
POSITION_SIMULATOR = os.getenv('POSITION_SIMULATOR', '0').lower() in ('1', 'true', 'yes')

from shared.device import device_id, device_name

DEVICE_ID = device_id()
DEVICE_NAME = device_name()

# Camera: the server puts the camera into the video mode and sets the quality before every recording
CAMERA_SET_MODE = os.getenv('CAMERA_SET_MODE', '1').lower() in ('1', 'true', 'yes')  # a camera left in a photo mode refuses to record
CAMERA_QUALITY = os.getenv('CAMERA_QUALITY', '5.7K30')  # 4K24..4K100, 5.7K24..5.7K60, 6K24..6K30, 8K24..8K30, 11K30; empty = leave as set
CAMERA_BITRATE_MBPS = float(os.getenv('CAMERA_BITRATE_MBPS') or 0)  # 0 = the camera default
CAMERA_SYNC_CLOCK = os.getenv('CAMERA_SYNC_CLOCK', '1').lower() in ('1', 'true', 'yes')  # only when the GNSS confirms the system clock
CAMERA_INFO_REFRESH_S = float(os.getenv('CAMERA_INFO_REFRESH_S') or 5)  # how old the battery, card and recording state in /status may be
