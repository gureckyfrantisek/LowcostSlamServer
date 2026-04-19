# Port settings, baud rates,...
import os
from dotenv import load_dotenv

load_dotenv()

# Config variables
BASE_PATH = "/media/pi" if not os.getenv('BASE_PATH') else os.getenv('BASE_PATH')
