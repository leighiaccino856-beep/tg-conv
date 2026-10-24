from __future__ import annotations


def human_size(num_bytes: int | float) -> str:
    """Человекочитаемый размер: 934 КБ, 4.8 МБ."""
    value = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}".replace(".0", "")
        value /= 1024
    return f"{value:.1f} ГБ"
