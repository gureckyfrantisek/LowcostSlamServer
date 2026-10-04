# Port settings, baud rates,...
import os
from dotenv import load_dotenv
from getpass import getuser


load_dotenv()

# Config variables
LOCAL_DATA_PATH = "/tmp/projects" if not os.getenv('LOCAL_DATA_PATH') else os.getenv('LOCAL_DATA_PATH')
BASE_USB_PATH = f"/media/{getuser()}" if not os.getenv('BASE_USB_PATH') else os.getenv('BASE_USB_PATH')

# Serial port settings
GNSS_PORT = "/dev/ttyACM0" if not os.getenv('GNSS_PORT') else os.getenv('GNSS_PORT')
GNSS_BAUD_RATE = 115200 if not os.getenv('GNSS_BAUD_RATE') else os.getenv('GNSS_BAUD_RATE')

IMU_PORT_1 = "/dev/ttyACM1" if not os.getenv('IMU_PORT_1') else os.getenv('IMU_PORT_1')
IMU_BAUD_RATE_1 = 115200 if not os.getenv('IMU_BAUD_RATE_1') else os.getenv('IMU_BAUD_RATE_1')

IMU_PORT_2 = "/dev/ttyACM2" if not os.getenv('IMU_PORT_2') else os.getenv('IMU_PORT_2')
IMU_BAUD_RATE_2 = 115200 if not os.getenv('IMU_BAUD_RATE_2') else os.getenv('IMU_BAUD_RATE_2')