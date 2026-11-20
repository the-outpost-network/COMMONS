"""Unit spellings, so "m3", "cubic metres" and "m³" count as the same unit.

General measurement words, not community settings: a resource's own unit
comes from the database; this only decides whether what a member typed means
that unit.
"""

from __future__ import annotations

_SAME = [
    {"l", "liter", "liters", "litre", "litres", "ltr"},
    {"ml", "milliliter", "milliliters", "millilitre", "millilitres"},
    {"m³", "m3", "cbm", "cubic meter", "cubic meters", "cubic metre", "cubic metres"},
    {"m²", "m2", "square meter", "square meters", "square metre", "square metres"},
    {"kg", "kilo", "kilos", "kilogram", "kilograms", "kilogramme", "kilogrammes"},
    {"g", "gram", "grams", "gramme", "grammes"},
    {"t", "ton", "tons", "tonne", "tonnes"},
    {"%", "percent", "per cent", "pct"},
    {"kwh", "kilowatt hour", "kilowatt hours"},
]


def _norm(unit: str) -> str:
    return " ".join(unit.replace(".", " ").split()).lower()


def canonical(unit: str) -> str:
    u = _norm(unit)
    for group in _SAME:
        if u in group:
            return sorted(group)[0]
    # "baskets" and "basket", "bottles" and "bottle".
    return u[:-1] if len(u) > 3 and u.endswith("s") else u


def same(a: str, b: str) -> bool:
    return canonical(a) == canonical(b)


def known(unit: str) -> bool:
    """A spelling of a measurement unit we know ("m3", "litres", "%")."""
    u = _norm(unit)
    return any(u in group for group in _SAME)


def spellings(unit: str) -> set[str]:
    """Every way of writing `unit` we know ("l": "litres", "ltr", …)."""
    u = _norm(unit)
    for group in _SAME:
        if u in group:
            return set(group)
    return {u, f"{u}s"}
