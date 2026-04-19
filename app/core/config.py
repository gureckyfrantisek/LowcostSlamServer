# Port settings, baud rates,...
import os
from dotenv import load_dotenv

load_dotenv()

# Config variables
USB_PATH = "/media/pi" if not os.getenv('USB_PATH') else os.getenv('USB_PATH')
