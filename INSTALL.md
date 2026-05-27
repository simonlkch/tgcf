# tgcf Installation, Debug, Build, and Docker Guide

This guide explains how to run this repository locally with Python 3.13, debug it with a virtual environment, build the package, and run it with Docker.

## 1) Install with Python 3.13 venv (Windows PowerShell)

`pyproject.toml` requires Python `>=3.13,<3.14`, so use Python 3.13.

```powershell
# From repository root
py -3.13 -m venv .venv313
.\.venv313\Scripts\Activate.ps1

python -m pip install --upgrade pip wheel setuptools
python -m pip install -e .

# This repo now pins cryptg 0.6.0 for Python 3.13 on Windows.
# If you reinstall after updating the lockfile, the editable install should pick it up automatically.

# Verify CLI is available
tgcf --version
```

If you need OCR/watermark features locally (non-Docker), also install system tools:

- `ffmpeg`
- `tesseract-ocr`

## 2) Debug with venv Python 3.13

Use the same activated `.venv313` environment in VS Code.

1. Select interpreter: `.venv313\Scripts\python.exe`
2. Set environment password in `.env`:

```env
PASSWORD=your-strong-password-here
```

### Debug Web UI

```powershell
.\.venv313\Scripts\python run_web_ui.py
```

### Debug CLI

```powershell
# live mode
.\.venv313\Scripts\python run_tgcf.py live

# past mode
.\.venv313\Scripts\python run_tgcf.py past
```

You can also run installed entry points:

```powershell
tgcf-web
tgcf live
tgcf past
```

## 3) Build this project (wheel)

This project uses Poetry for packaging.

```powershell
.\.venv313\Scripts\python -m pip install poetry

# Build distributables into dist/
poetry build

# Optional: install built wheel into current venv
python -m pip install --force-reinstall .\dist\*.whl
```

Build output files:

- `dist/*.whl`
- `dist/*.tar.gz`

## 4) Build with Docker and use it

The included `Dockerfile` already installs runtime OS dependencies (`ffmpeg`, `tesseract-ocr`) and starts `tgcf-web`.

### Build image

```powershell
docker build -t tgcf:local .
```

### Run container

Create `.env` in repo root:

```env
PASSWORD=your-strong-password-here
```

Then run:

```powershell
docker run --rm -p 8501:8501 --env-file .env tgcf:local
```

Open the web UI at:

- `http://localhost:8501`

### Run in detached mode

```powershell
docker run -d --name tgcf -p 8501:8501 --env-file .env tgcf:local
docker logs -f tgcf
docker stop tgcf
```

## 5) Quick sanity checks

```powershell
# Local
tgcf --version

# Docker
docker ps
docker logs tgcf
```
