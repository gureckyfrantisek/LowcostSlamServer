# Port settings, baud rates,...
import os
from dotenv import load_dotenv
from getpass import getuser


load_dotenv()

# Config variables
LOCAL_DATA_PATH = "/tmp/projects" if not os.getenv('LOCAL_DATA_PATH') else os.getenv('LOCAL_DATA_PATH')
BASE_USB_PATH = "/media/{getuser()}" if not os.getenv('BASE_USB_PATH') else os.getenv('BASE_USB_PATH')