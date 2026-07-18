"""
Device Auth
============
Handles Bridge's permanent device identity.

First run:  user types the 6-digit pairing code shown in Clarity ->
            we exchange it once for a device_id + device_secret via
            Cloud's /pair endpoint -> saved locally, forever.
Every run after that: we just load the saved credentials. No token,
no login, no environment variable to set by hand.

Credentials are stored per-OS in the standard app-data location so
they survive a reinstall of Bridge into a different folder:
  Windows: %APPDATA%\\ClarityBridge\\device.json
  macOS:   ~/Library/Application Support/ClarityBridge/device.json
  Linux:   ~/.config/ClarityBridge/device.json
"""

import json
import os
import platform
import socket
import urllib.request
import urllib.error


def _config_dir() -> str:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "ClarityBridge")


def _config_path() -> str:
    return os.path.join(_config_dir(), "device.json")


def load_device_credentials():
    """Returns (device_id, device_secret) or (None, None) if not paired yet."""
    try:
        with open(_config_path(), "r") as f:
            data = json.load(f)
        return data.get("device_id"), data.get("device_secret")
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None


def save_device_credentials(device_id: str, device_secret: str) -> None:
    os.makedirs(_config_dir(), exist_ok=True)
    with open(_config_path(), "w") as f:
        json.dump({"device_id": device_id, "device_secret": device_secret}, f)


def pair_with_code(code: str, cloud_http_url: str, platform_name: str = "MT5"):
    """
    Exchange a 6-digit pairing code (shown in Clarity's browser UI) for a
    permanent device_id + device_secret. Raises RuntimeError with a
    human-readable message on failure.
    """
    body = json.dumps({
        "code":        code.strip(),
        "device_name": socket.gethostname(),
        "platform":    platform_name,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{cloud_http_url.rstrip('/')}/pair",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            err = str(e)
        raise RuntimeError(err)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Clarity Cloud: {e}")

    return data["device_id"], data["device_secret"]


def prompt_for_pairing(cloud_http_url: str, platform_name: str = "MT5"):
    """Interactive first-run pairing prompt. Returns (device_id, device_secret)."""
    print("\nClarity Bridge isn't connected to your account yet.")
    print("Open Clarity in your browser -> Algo -> Connect Broker to get a pairing code.\n")

    while True:
        code = input("Enter the 6-digit code: ").strip()
        if not (code.isdigit() and len(code) == 6):
            print("That doesn't look like a 6-digit code — try again.\n")
            continue
        try:
            device_id, device_secret = pair_with_code(code, cloud_http_url, platform_name)
            save_device_credentials(device_id, device_secret)
            print("\nPaired successfully. Clarity Bridge is now connected to your account.\n")
            return device_id, device_secret
        except RuntimeError as e:
            print(f"Pairing failed: {e}\n")
