def normalize_title(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    return cleaned.lower()
