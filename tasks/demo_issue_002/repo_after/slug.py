def slugify(value: str) -> str:
    if value is None:
        return ""
    cleaned = value.strip().lower()
    return cleaned.replace(" ", "-")
