"""Windows LAN interfaces shared by the launcher and UDP discovery."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import subprocess


def windows_lan_interfaces() -> list[ipaddress.IPv4Interface]:
    """Read active hardware interfaces, without DNS or an Internet probe.

    Use adapter metadata rather than localized names or private subnet ranges:
    a VMware/Hyper-V host-only address can look just like a Wi-Fi address.
    Fail closed if enumeration fails; never fall back to all ipconfig addresses.
    """
    command = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    script = """
$ErrorActionPreference = 'Stop'
$items = @(Get-NetIPConfiguration -All -ErrorAction Stop | ForEach-Object {
    $adapter = $_.NetAdapter
    foreach ($address in $_.IPv4Address) {
        [pscustomobject]@{
            IPAddress = $address.IPAddress
            PrefixLength = $address.PrefixLength
            HardwareInterface = $adapter.HardwareInterface
            Status = [string]$adapter.Status
        }
    }
})
ConvertTo-Json -InputObject $items -Compress
"""
    try:
        result = subprocess.run(
            [command, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, check=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        records = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(records, list):
            return []
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return []

    interfaces = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("HardwareInterface") is not True or record.get("Status") != "Up":
            continue
        try:
            interface = ipaddress.IPv4Interface(
                f"{record['IPAddress']}/{record['PrefixLength']}")
        except (KeyError, TypeError, ValueError):
            continue
        address = interface.ip
        if (address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_unspecified or int(address) >> 24 == 0
                or address == ipaddress.IPv4Address("255.255.255.255")):
            continue
        interfaces.add(interface)
    return sorted(interfaces, key=lambda item: (int(item.ip), item.network.prefixlen))
