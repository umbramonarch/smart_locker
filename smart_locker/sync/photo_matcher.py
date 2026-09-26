"""
File: photo_matcher.py
Description: Shared exact, case-insensitive model-to-photo matching rules.
Project: smart_locker/sync
Notes: A photo filename stem identifies a model, never a PM number.
"""

def model_key(value: str | None) -> str:
    return (value or "").casefold()


def matches_model(photo_stem: str, model: str | None) -> bool:
    return bool(model_key(model)) and model_key(photo_stem) == model_key(model)


def photo_matches_device(photo_stem: str, device) -> bool:
    return matches_model(photo_stem, device.model)
