"""Package tgcf.

The ultimate tool to automate custom telegram message forwarding.
https://github.com/aahnik/tgcf
"""

# 临时版本号，用于开发环境
try:
    from importlib.metadata import version, PackageNotFoundError
    __version__ = version(__package__)
except (ImportError, PackageNotFoundError):
    __version__ = "dev-1.1.8"
