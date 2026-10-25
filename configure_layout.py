from __future__ import annotations

import argparse
from typing import Any

from settings import CONFIG_PATH, load_settings, save_settings
from screen_edges import validate_edge_monitors


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
    choices = ("right", "left", "top", "bottom", "off", "multi")
    current_choice = choices.index(current) + 1 if current in choices else 1

    print("Screen layout")
    print()
    print("  1. Windows on the right")
    print("  2. Windows on the left")
    print("  3. Windows above Mac")
    print("  4. Windows below Mac")
    print("  5. Disable edge switching")
    print("  6. Map edges to Windows monitor numbers")
    print()
    while True:
        value = input(f"Choose layout [current {current_choice}]: ").strip()
        if not value:
            return choices[current_choice - 1]
        if value in {"1", "2", "3", "4", "5", "6"}:
            return choices[int(value) - 1]
        print("ERROR: Enter a number from 1 to 6.")


def prompt_monitor_mappings(current: dict[str, int]) -> dict[str, int]:
    mappings = dict(current)
    for edge in ("top", "right", "bottom", "left"):
        while True:
            value = input(f"Mac {edge} -> Windows monitor [current {mappings.get(edge, 'off')}; off to disable]: ").strip()
            if not value:
                break
            if value.lower() == "off":
                mappings.pop(edge, None)
                break
            try:
                mappings.update(parse_monitor_mappings([f"{edge}={value}"]))
                break
            except ValueError as exc:
                print(f"ERROR: {exc}")
    return mappings


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
    updated["edge_monitors"] = {}
    updated["edge_margin"] = validate_edge_margin(edge_margin)
    return updated


def parse_monitor_mappings(values: list[str]) -> dict[str, int]:
    mappings: dict[str, int] = {}
    for value in values:
        edge, separator, number = value.partition("=")
        edge = edge.strip().lower()
        if not separator or not number.strip().isascii() or not number.strip().isdigit():
            raise ValueError("Use --map EDGE=NUMBER, for example --map top=1 --map right=2")
        if edge in mappings:
            raise ValueError(f"Duplicate mapping for {edge}")
        mappings[edge] = int(number)
    return validate_edge_monitors(mappings)


def update_monitor_layout(config: dict[str, Any], mappings: dict[str, int],
                          edge_margin: int) -> dict[str, Any]:
    updated = dict(config)
    updated["edge_monitors"] = validate_edge_monitors(mappings)
    updated["auto_switch_edge"] = "off"
    updated["edge_margin"] = validate_edge_margin(edge_margin)
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Place Windows around the Mac for edge switching."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--position",
        choices=("left", "right", "top", "bottom", "off"),
        help="Windows screen position relative to the Mac.",
    )
    mode.add_argument("--map", dest="mappings", action="append", metavar="EDGE=NUMBER",
                      help="Map a Mac edge to a Windows monitor; repeat for multiple edges.")
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
    interactive = args.position is None and args.edge_margin is None and args.mappings is None

    try:
        if args.position is not None:
            position = validate_position(args.position)
        elif interactive:
            position = prompt_position("multi" if config.get("edge_monitors") else current_position)
        else:
            position = current_position
        edge_margin = (
            validate_edge_margin(args.edge_margin)
            if args.edge_margin is not None
            else prompt_edge_margin(current_margin) if interactive else current_margin
        )
        if args.mappings is not None:
            config = update_monitor_layout(config, parse_monitor_mappings(args.mappings), edge_margin)
        elif position == "multi":
            config = update_monitor_layout(config, prompt_monitor_mappings(config.get("edge_monitors", {})), edge_margin)
        elif not interactive and args.position is None:
            config["edge_margin"] = edge_margin
        else:
            config = update_layout(config, position, edge_margin)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    save_settings(config, CONFIG_PATH)
    print()
    if config.get("edge_monitors"):
        for edge, number in config["edge_monitors"].items():
            print(f"Saved mapping: Mac {edge} -> Windows monitor {number}")
    else:
        print(f"Saved layout: {POSITION_LABELS[config['auto_switch_edge']]}")
    print(f"Edge trigger width: {edge_margin}")
    print("Restart mac_client.py to apply the new layout.")


if __name__ == "__main__":
    main()
