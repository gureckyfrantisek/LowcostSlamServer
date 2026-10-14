# Port settings, baud rates,...
import os
from dotenv import load_dotenv
from getpass import getuser


load_dotenv()

# Config variables
LOCAL_DATA_PATH = LOCAL_DATA_PATH = os.path.expanduser("~/measurements") if not os.getenv('LOCAL_DATA_PATH') else os.getenv('LOCAL_DATA_PATH')
BASE_USB_PATH = f"/media/{getuser()}" if not os.getenv('BASE_USB_PATH') else os.getenv('BASE_USB_PATH')

# Serial port settings
GNSS_PORT = "/dev/ttyACM0" if not os.getenv('GNSS_PORT') else os.getenv('GNSS_PORT')
GNSS_BAUD_RATE = 115200 if not os.getenv('GNSS_BAUD_RATE') else os.getenv('GNSS_BAUD_RATE')

IMU_PORT_1 = "/dev/ttyS2" if not os.getenv('IMU_PORT_1') else os.getenv('IMU_PORT_1') # GPIO 8 and 10 on Radxa
IMU_BAUD_RATE_1 = 115200 if not os.getenv('IMU_BAUD_RATE_1') else os.getenv('IMU_BAUD_RATE_1')

IMU_PORT_2 = "/dev/ttyACM2" if not os.getenv('IMU_PORT_2') else os.getenv('IMU_PORT_2')
IMU_BAUD_RATE_2 = 115200 if not os.getenv('IMU_BAUD_RATE_2') else os.getenv('IMU_BAUD_RATE_2')

IMU_CALIBRATION_PATH_1 = os.path.expanduser("~/imu_calibration_1.json") if not os.getenv('IMU_CALIBRATION_PATH_1') else os.getenv('IMU_CALIBRATION_PATH_1')
IMU_CALIBRATION_PATH_2 = os.path.expanduser("~/imu_calibration_2.json") if not os.getenv('IMU_CALIBRATION_PATH_2') else os.getenv('IMU_CALIBRATION_PATH_2')

# IMU calibration, see docs/imu_calibration.md
IMU_CAL_MAX_AGE_DAYS = 30.0 if not os.getenv('IMU_CAL_MAX_AGE_DAYS') else float(os.getenv('IMU_CAL_MAX_AGE_DAYS'))  # older calibration is reported as outdated

# Static steps (accelerometer + gyroscope, device stationary)
IMU_CAL_STILL_SECONDS = 3.0 if not os.getenv('IMU_CAL_STILL_SECONDS') else float(os.getenv('IMU_CAL_STILL_SECONDS'))  # measuring time of one static step
IMU_CAL_MIN_STILL_SAMPLES = 20 if not os.getenv('IMU_CAL_MIN_STILL_SAMPLES') else int(os.getenv('IMU_CAL_MIN_STILL_SAMPLES'))
IMU_CAL_MAX_ACCEL_STD_MG = 5.0 if not os.getenv('IMU_CAL_MAX_ACCEL_STD_MG') else float(os.getenv('IMU_CAL_MAX_ACCEL_STD_MG'))  # more than this means the device moved
IMU_CAL_MAX_GYRO_STD_DPS = 0.5 if not os.getenv('IMU_CAL_MAX_GYRO_STD_DPS') else float(os.getenv('IMU_CAL_MAX_GYRO_STD_DPS'))
IMU_CAL_MAX_GRAVITY_ERROR = 0.15 if not os.getenv('IMU_CAL_MAX_GRAVITY_ERROR') else float(os.getenv('IMU_CAL_MAX_GRAVITY_ERROR'))  # |a| may differ from 1 g by this fraction
IMU_CAL_MAX_TILT_DEG = 35.0 if not os.getenv('IMU_CAL_MAX_TILT_DEG') else float(os.getenv('IMU_CAL_MAX_TILT_DEG'))  # allowed error of the requested orientation
IMU_CAL_MIN_SCALE = 0.9 if not os.getenv('IMU_CAL_MIN_SCALE') else float(os.getenv('IMU_CAL_MIN_SCALE'))  # accepted accelerometer scale correction
IMU_CAL_MAX_SCALE = 1.1 if not os.getenv('IMU_CAL_MAX_SCALE') else float(os.getenv('IMU_CAL_MAX_SCALE'))

# Magnetometer step (device moving)
IMU_CAL_MAG_SECONDS = 45.0 if not os.getenv('IMU_CAL_MAG_SECONDS') else float(os.getenv('IMU_CAL_MAG_SECONDS'))  # default, the endpoint can override it
IMU_CAL_MAG_MIN_SAMPLES = 100 if not os.getenv('IMU_CAL_MAG_MIN_SAMPLES') else int(os.getenv('IMU_CAL_MAG_MIN_SAMPLES'))
IMU_CAL_MAG_MIN_OCTANTS = 6 if not os.getenv('IMU_CAL_MAG_MIN_OCTANTS') else int(os.getenv('IMU_CAL_MAG_MIN_OCTANTS'))  # of 8, how much of the sphere the rotation must cover
IMU_CAL_MAG_MAX_RESIDUAL = 0.05 if not os.getenv('IMU_CAL_MAG_MAX_RESIDUAL') else float(os.getenv('IMU_CAL_MAG_MAX_RESIDUAL'))  # fit residual relative to the field radius
IMU_CAL_LATITUDE = 50.0755 if not os.getenv('IMU_CAL_LATITUDE') else float(os.getenv('IMU_CAL_LATITUDE'))  # Prague, until the NMEA position is used
IMU_CAL_LONGITUDE = 14.4378 if not os.getenv('IMU_CAL_LONGITUDE') else float(os.getenv('IMU_CAL_LONGITUDE'))
IMU_CAL_ALTITUDE_M = 190.0 if not os.getenv('IMU_CAL_ALTITUDE_M') else float(os.getenv('IMU_CAL_ALTITUDE_M'))
IMU_CAL_IGRF_MAX_YEAR = 2025.0 if not os.getenv('IMU_CAL_IGRF_MAX_YEAR') else float(os.getenv('IMU_CAL_IGRF_MAX_YEAR'))  # last year the pyIGRF model supports
IMU_CAL_MAG_STORED_POINTS =600 if not os.getenv('IMU_CAL_MAG_STORED_POINTS') else int(os.getenv('IMU_CAL_MAG_STORED_POINTS'))  # raw mag samples kept in the step results file
