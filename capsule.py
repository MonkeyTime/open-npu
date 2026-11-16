"""Capsule intégrée square-fp16-v1, partagée par le serveur et l'agent."""

import math

CAPSULE = "square-fp16-v1"
WIDTH = 16
MAX_VALUES = 4096


def valid_values(values, limit=MAX_VALUES):
    return isinstance(values, list) and 0 < len(values) <= limit and all(
        type(value) in (int, float) and -200 <= value <= 200 and math.isfinite(value)
        for value in values
    )
