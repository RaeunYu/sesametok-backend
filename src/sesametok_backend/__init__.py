"""sesametok-backend: macOS-native KakaoTalk messaging backend.

This package is the project's single local process.
It owns exactly four responsibilities:

* read the logged-in KakaoTalk desktop database (read-only, SQLCipher),
* archive messages locally and detect changes,
* expose a loopback-only HTTP/UDS API plus an event stream,
* drive the KakaoTalk UI through macOS Accessibility to send messages.

It deliberately implements no chat UI and no third-party messenger adapter;
those are downstream interface projects that consume this API.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
