# tgcf Usage Guide

A comprehensive guide to using tgcf for Telegram chat management and automation.

## Introduction
tgcf is a powerful tool for managing Telegram chats with advanced filtering and processing capabilities. It allows you to automate chat management, filter messages, and process content using a variety of plugins.

This guide will walk you through installation, configuration, and usage of both the command-line interface (CLI) and web UI.

## Getting Started

### Step 1: Install tgcf
Follow the installation instructions in the [Installation](#installation) section.

### Step 2: Configure API Credentials
1. Go to [my.telegram.org](https://my.telegram.org) and log in with your Telegram account.
2. Create a new application to get your `API_ID` and `API_HASH`.
3. Create a `.env` file in the project root and add these credentials.

### Step 3: Configure Chats
1. Run tgcf once to generate the configuration file:
   ```bash
   # Windows
   .\tgcf_cli.bat run-live

   # Unix-like systems
   make run-live
   ```
2. Stop tgcf and edit `tgcf.config.json` to add your source and target chats.
3. Add chat IDs and set their types (`source` or `target`).

### Step 4: Start Using tgcf
Choose your preferred method:
- [CLI Usage](#using-the-cli)
- [Web UI](#running-the-web-ui)

## Installation

### Prerequisites
- Python 3.8 or higher
- Poetry (for dependency management)
- Git (optional)

### Steps
1. Clone the repository (or download the source code):
   ```bash
   git clone https://github.com/yourusername/tgcf.git
   cd tgcf
   ```

2. Install dependencies using Poetry:
   ```bash
   poetry install
   ```

3. Create a virtual environment (recommended):
   ```bash
   poetry shell
   ```

## Configuration

### Environment Variables
Create a `.env` file in the project root with the following variables:

```env
# Telegram API credentials (required)
API_ID=your_api_id
API_HASH=your_api_hash

# Bot token (optional, required if using bot mode)
BOT_TOKEN=your_bot_token

# Web UI settings
PASSWORD=your_password  # Default: tgcf
PORT=8502  # Default: 8502

# Application settings
TGCF_MODE=live  # live or past
LOUD=false  # Enable verbose logging
FAKE=false  # Enable fake mode for testing

# Database settings
DB_PATH=tgcf.db  # Path to SQLite database
```

### Config File
`tgcf.config.json` is created automatically on first run. Here's a complete example with explanations:

```json
{
  "version": "1.0",
  "chats": [
    {"id": -1001234567890, "name": "Source Chat", "type": "source"},
    {"id": -1009876543210, "name": "Target Chat", "type": "target"}
  ],
  "plugins": {
    "caption": {"enabled": false},
    "filter": {
      "enabled": true,
      "patterns": ["spam", "offensive"],
      "action": "delete"  # delete or ignore
    },
    "fmt": {"enabled": false},
    "mark": {"enabled": false},
    "ocr": {"enabled": false},
    "replace": {"enabled": false},
    "sender": {"enabled": true}
  },
  "settings": {
    "max_messages": 1000,
    "delay": 1,
    "delete_after_processing": false
  },
  "web_ui": {
    "enabled": true,
    "theme": "light"
  }
}
```

#### Configuration Options
- `chats`: List of chat configurations
  - `id`: Chat ID
  - `name`: Friendly name (optional)
  - `type`: `source` or `target`

- `plugins`: Plugin configurations (see [Using Plugins](#using-plugins) section)

- `settings`: Application settings
  - `max_messages`: Maximum number of messages to process
  - `delay`: Delay between message processing (seconds)
  - `delete_after_processing`: Delete messages after processing

- `web_ui`: Web interface settings
  - `enabled`: Enable/disable web UI
  - `theme`: `light` or `dark` theme

## Using the CLI

tgcf provides a command-line interface for easy automation and integration.

### Windows
Use the `tgcf_cli.bat` script:

```batch
# Show help message
.	tgcf_cli.bat help

# Run in live mode
.	tgcf_cli.bat run-live

# Run in past mode
.	tgcf_cli.bat run-past

# Run in live mode with verbose output
.	tgcf_cli.bat run-live-verbose

# Run in past mode with verbose output
.	tgcf_cli.bat run-past-verbose
```

### Unix-like Systems (Linux/Mac)
Use the Makefile targets:

```bash
# Show help message
make help

# Run in live mode
make run-live

# Run in past mode
make run-past

# Run in live mode with verbose output
make run-live-verbose

# Run in past mode with verbose output
make run-past-verbose
```

## Running the Web UI

tgcf includes a web interface for easier configuration and monitoring.

### Manual Launch

```bash
# Windows
# Activate the virtual environment
.venv\Scripts\activate.bat

# Run the web UI
python final_launch.py

# Unix-like systems
# Activate the virtual environment
poetry shell

# Run the web UI
python final_launch.py
```

The web UI will be available at `http://localhost:8502`. You can access it with your web browser using the default password `tgcf` (or the one you set in `.env`).

## Using Plugins
tgcf supports a powerful plugin system that allows you to extend its functionality.

### Available Plugins
- **caption**: Add captions to media files
- **filter**: Filter messages based on content
- **fmt**: Format text messages
- **mark**: Add markers to messages
- **ocr**: Extract text from images
- **replace**: Replace text patterns
- **sender**: Send messages to specific chats

### Enabling Plugins
Configure plugins in `tgcf.config.json`:
```json
{
  "plugins": {
    "filter": {
      "enabled": true,
      "patterns": ["spam", "offensive"]
    },
    "ocr": {
      "enabled": true,
      "languages": ["en", "es"]
    }
  }
}
```

## Examples

### Basic Usage Example
1. Start tgcf in live mode:
   ```bash
   # Windows
   .\tgcf_cli.bat run-live

   # Unix-like systems
   make run-live
   ```

2. Monitor the output for processed messages and any warnings.

### Advanced Filtering
Configure filters in `tgcf.config.json` to:
- Filter messages by content
- Block specific users
- Automatically reply to certain messages
- Forward messages between chats

## Troubleshooting

### Common Issues

1. **Login Issues**
   - Ensure your API credentials in `.env` are correct
   - Check that your Telegram account is not restricted

2. **Web UI Not Accessible**
   - Verify the Streamlit server is running
   - Check if the port 8502 is blocked by a firewall

3. **Performance Problems**
   - Try running with fewer active chats
   - Increase verbosity to identify bottlenecks: `tgcf_cli.bat run-live-verbose`

### Getting Help
If you encounter issues not covered in this guide:
- Check the [GitHub Issues](https://github.com/yourusername/tgcf/issues) page
- Join the Telegram support group (link in README.md)

## Updating tgcf

```bash
# If using git
# Pull the latest changes
git pull

# Update dependencies
poetry install

# If not using git
# Download the latest release from the project repository
# Replace with actual download link
wget https://github.com/username/tgcf/archive/refs/tags/latest.tar.gz
# Extract and install
```

## License

tgcf is licensed under the [MIT License](LICENSE).