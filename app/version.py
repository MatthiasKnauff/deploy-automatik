import os

# Wird beim Build über die Pipeline gesetzt (APP_VERSION=vX.Y.Z); ohne Pipeline gilt der Wert hier.
APP_VERSION = os.environ.get("APP_VERSION", "1.2.3").lstrip("vV") or "1.2.3"
