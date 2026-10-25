from __future__ import annotations

import argparse
from typing import Any

from settings import CONFIG_PATH, load_settings, save_settings


POSITIONS = {"left", "right", "top", "bottom", "off"}
POSITION_LABELS = {
    "right": "Windows is to the right of Mac",
    "left": "Windows is to the left of Mac",
    "top": "Windows is above Mac",
    "bottom": "Windows is below Mac",
    "off": "Edge switching is disabled",
}


def validate_position(position: str) -> str:
    value = position.strip().lower()
    if value not in POSITIONS:
        raise ValueError("Position must be left, right, top, bottom, or off.")
    return value


def validate_edge_margin(edge_margin: str | int) -> int:
    try:
        value = int(edge_margin)
    except (TypeError, ValueError) as exc:
        raise ValueError("Edge margin must be a number.") from exc
    if not 1 <= value <= 100:
        raise ValueError("Edge margin must be between 1 and 100.")
    return value


def prompt_position(current: str) -> str:
    choices = ("right", "left", "top", "bottom", "off")
    current_choice = choices.index(current) + 1 if current in choices else 1

    print("Screen layout")
    print()
    print("  1. Windows on the right")
    print("  2. Windows on the left")
    print("  3. Windows above Mac")
    print("  4. Windows below Mac")
    print("  5. Disable edge switching")
    print()
    while True:
        value = input(f"Choose layout [current {current_choice}]: ").strip()
        if not value:
            return choices[current_choice - 1]
        if value in {"1", "2", "3", "4", "5"}:
            return choices[int(value) - 1]
        print("ERROR: Enter a number from 1 to 5.")


def prompt_edge_margin(current: int) -> int:
    while True:
        value = input(f"Edge trigger width [current {current}]: ").strip()
        try:
            return validate_edge_margin(current if not value else value)
        except ValueError as exc:
            print(f"ERROR: {exc}")


def update_layout(
    config: dict[str, Any],
    position: str,
    edge_margin: int,
) -> dict[str, Any]:
    updated = dict(config)
    updated["auto_switch_edge"] = validate_position(position)
    updated["edge_margin"] = validate_edge_margin(edge_margin)
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Place Windows around the Mac for edge switching."
    )
    parser.add_argument(
        "--position",
        choices=("left", "right", "top", "bottom", "off"),
        help="Windows screen position relative to the Mac.",
    )
    parser.add_argument(
        "--edge-margin",
        type=int,
        help="Trigger width at the Mac screen edge (1-100).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_settings(CONFIG_PATH)
    current_position = str(config.get("auto_switch_edge", "off"))
    current_margin = int(config.get("edge_margin", 2))
    interactive = args.position is None and args.edge_margin is None

    try:
        position = (
            validate_position(args.position)
            if args.position is not None
            else prompt_position(current_position) if interactive else current_position
        )
        edge_margin = (
            validate_edge_margin(args.edge_margin)
            if args.edge_margin is not None
            else prompt_edge_margin(current_margin) if interactive else current_margin
        )
        config = update_layout(config, position, edge_margin)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    save_settings(config, CONFIG_PATH)
    print()
    print(f"Saved layout: {POSITION_LABELS[position]}")
    print(f"Edge trigger width: {edge_margin}")
    print("Restart mac_client.py to apply the new layout.")


if __name__ == "__main__":
    main()
