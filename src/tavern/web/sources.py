"""Public citation metadata, with no upstream identifiers or credential-bearing URLs."""
from urllib.parse import urlsplit


def source_links(rows, *, strict=False):
    result, seen = [], set()
    for row in rows:
        try:
            url = row.get("url")
            if not isinstance(url, str):
                raise ValueError()
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
        except (ValueError, AttributeError):
            if strict:
                raise ValueError("服务返回的来源网址无效") from None
            continue
        if url not in seen:
            seen.add(url)
            title = row.get("title")
            result.append({"url": url, "title": title if isinstance(title, str) and title else parsed.hostname})
    return result


def protect_secrets(value, secrets):
    """Redact known credentials only; source examples that merely resemble tokens stay intact."""
    if isinstance(value, dict):
        return {k: protect_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [protect_secrets(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value
