"""Narrow native boundaries; desktop imports never load Chaquopy."""

import os


def is_android():
    return os.environ.get("TAVERN_PLATFORM") == "android"


def _native():
    from java import jclass

    return jclass("com.terrarium.tavern.AndroidPlatform")


def open_browser(url):
    if is_android():
        return bool(_native().openBrowser(url))
    import webbrowser

    return webbrowser.open(url)


def protect_credentials(data: bytes) -> bytes:
    try:
        return str(_native().encryptCredentials(data.decode("utf-8"))).encode("ascii")
    except Exception as error:
        # Keystore may be unavailable or its key invalidated; never write plaintext as a fallback.
        raise ValueError("Android 无法保护 ChatGPT 凭据，未保存连接") from error


def unprotect_credentials(data: bytes) -> bytes:
    try:
        return str(_native().decryptCredentials(data.decode("ascii"))).encode("utf-8")
    except Exception as error:
        # Authenticated decryption failures must preserve the original credential file.
        raise ValueError("Android 无法解密原 ChatGPT 凭据，请重新连接账号") from error
