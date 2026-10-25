"""Screen-edge geometry. Bounds use an exclusive right/bottom coordinate."""

from __future__ import annotations

from dataclasses import dataclass


OPPOSITE_EDGE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


@dataclass(frozen=True)
class ScreenBounds:
    left: float
    top: float
    right: float
    bottom: float

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def crossing(self, edge: str, x: float, y: float, dx: float, dy: float,
                 margin: int) -> bool:
        if edge in {"left", "right"} and self.top <= y < self.bottom:
            return (edge == "left" and dx < 0 and x <= self.left + margin) or (
                edge == "right" and dx > 0 and x >= self.right - 1 - margin
            )
        if edge in {"top", "bottom"} and self.left <= x < self.right:
            return (edge == "top" and dy < 0 and y <= self.top + margin) or (
                edge == "bottom" and dy > 0 and y >= self.bottom - 1 - margin
            )
        return False

    def fraction(self, edge: str, x: float, y: float) -> float:
        value = ((y - self.top) / max(1, self.bottom - self.top - 1)
                 if edge in {"left", "right"}
                 else (x - self.left) / max(1, self.right - self.left - 1))
        return min(1.0, max(0.0, value))

    def landing(self, edge: str, fraction: float, margin: int) -> tuple[int, int]:
        fraction = min(1.0, max(0.0, fraction))
        width, height = self.right - self.left - 1, self.bottom - self.top - 1
        inset_x, inset_y = min(margin + 8, width / 2), min(margin + 8, height / 2)
        x, y = self.left + fraction * width, self.top + fraction * height
        if edge == "left":
            x = self.left + inset_x
        elif edge == "right":
            x = self.right - 1 - inset_x
        elif edge == "top":
            y = self.top + inset_y
        elif edge == "bottom":
            y = self.bottom - 1 - inset_y
        else:
            raise ValueError(f"Unknown screen edge: {edge}")
        return round(x), round(y)
