# Base image ships Python + the Playwright Python package + Chromium binaries
# for the matching version. When you bump the tag, also bump `playwright` in
# requirements.txt to the same version.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY deepstate_screenshot.py ./

VOLUME ["/out"]

ENTRYPOINT ["python", "deepstate_screenshot.py", "-o", "/out"]
