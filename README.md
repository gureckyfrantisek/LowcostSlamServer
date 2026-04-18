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
The server needs sudo permissions for USB port access.
```bash
sudo .venv/bin/uvicorn app.main:app --reload
```

### Try it out
Open the generated docs in [http://127.0.0.1:8000/docs#](http://127.0.0.1:8000/docs#) and try hitting the endpoints there.