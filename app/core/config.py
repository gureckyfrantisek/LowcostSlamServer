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