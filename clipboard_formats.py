"""Portable clipboard payloads. No native clipboard or network access here."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

from PIL import Image

IMAGE_MAX_BYTES = 16 * 1024 * 1024
RICH_MAX_BYTES = 16 * 1024 * 1024
FILES_MAX_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 32_000_000
MAX_FILES = 2000
LIMITS = {"image": IMAGE_MAX_BYTES, "rich": RICH_MAX_BYTES, "files": FILES_MAX_BYTES}


def image_to_png(data: bytes, *, png_only: bool = False) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        if png_only and image.format != "PNG":
            raise ValueError("expected a PNG image")
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("image exceeds 32 million pixels")
        output = io.BytesIO()
        image.convert("RGBA").save(output, "PNG")
    result = output.getvalue()
    if len(result) > IMAGE_MAX_BYTES:
        raise ValueError("PNG image exceeds 16 MiB")
    return result


def png_to_dib(data: bytes) -> bytes:
    # CF_DIB works in older Windows applications; PNG retains full alpha.
    with Image.open(io.BytesIO(data)) as image:
        rgba = image.convert("RGBA")
        rgb = Image.new("RGB", image.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        output = io.BytesIO()
        rgb.save(output, "BMP")
    return output.getvalue()[14:]


def pack_rich(text: str, html: bytes | None, rtf: bytes | None) -> bytes:
    value = {"text": text, "html": base64.b64encode(html or b"").decode("ascii"),
             "rtf": base64.b64encode(rtf or b"").decode("ascii")}
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(data) > RICH_MAX_BYTES:
        raise ValueError("rich text exceeds 16 MiB")
    return data


def unpack_rich(data: bytes) -> tuple[str, bytes, bytes]:
    if len(data) > RICH_MAX_BYTES:
        raise ValueError("rich text exceeds 16 MiB")
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != {"text", "html", "rtf"}:
        raise ValueError("invalid rich text payload")
    if any(not isinstance(v, str) for v in value.values()):
        raise ValueError("invalid rich text fields")
    html = base64.b64decode(value["html"], validate=True)
    rtf = base64.b64decode(value["rtf"], validate=True)
    if not html and not rtf:
        raise ValueError("missing rich text format")
    return value["text"], html, rtf


def encode_cf_html(html: bytes) -> bytes:
    start_marker, end_marker = b"<!--StartFragment-->", b"<!--EndFragment-->"
    if start_marker not in html or end_marker not in html:
        html = b"<html><body>" + start_marker + html + end_marker + b"</body></html>"
    template = "Version:1.0\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\nEndFragment:{:010d}\r\n"
    start = len(template.format(0, 0, 0, 0).encode("ascii"))
    header = template.format(start, start + len(html),
                             start + html.index(start_marker) + len(start_marker),
                             start + html.index(end_marker)).encode("ascii")
    return header + html + b"\0"


def decode_cf_html(data: bytes) -> bytes:
    # Retain the HTML context (including CSS) and fragment markers across OSes.
    def offset(name: bytes) -> int:
        match = re.search(rb"(?:^|\r?\n)" + name + rb":\s*(-?\d+)", data[:4096])
        return int(match[1]) if match else -1
    for first, last in ((b"StartHTML", b"EndHTML"), (b"StartFragment", b"EndFragment")):
        start, end = offset(first), offset(last)
        if 0 <= start <= end <= len(data):
            return data[start:end]
    raise ValueError("invalid Windows HTML clipboard offsets")


def _safe_parts(name: str) -> tuple[str, ...]:
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or "\\" in name or "\0" in name:
        raise ValueError("unsafe file path")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    for part in parts:
        if (part in {".", ".."} or part.endswith((".", " "))
                or any(c in '<>:"|?*' or ord(c) < 32 for c in part)
                or part.split(".")[0].upper() in reserved):
            raise ValueError("file name is not portable between Mac and Windows")
    return parts


def pack_files(paths: list[str]) -> bytes:
    """Copy regular files/directories only; never follow symbolic links."""
    output = io.BytesIO()
    total = count = 0
    names: set[str] = set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        def add(path: Path, name: str) -> None:
            nonlocal total, count
            _safe_parts(name)
            key = name.casefold()
            if key in names:
                raise ValueError("duplicate file names (ignoring case)")
            names.add(key)
            count += 1
            if count > MAX_FILES:
                raise ValueError("too many clipboard files")
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                archive.writestr(name + "/", b"")
                for child in sorted(path.iterdir()):
                    add(child, name + "/" + child.name)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > FILES_MAX_BYTES:
                    raise ValueError("clipboard files exceed 100 MiB")
                # O_NOFOLLOW also closes the symlink-swap race at open time.
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as source, archive.open(name, "w") as target:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("only regular files can be copied")
                    copied = 0
                    while chunk := source.read(64 * 1024):
                        copied += len(chunk)
                        if copied > info.st_size:
                            raise ValueError("file changed during clipboard transfer; copy again")
                        target.write(chunk)
                    if copied != info.st_size:
                        raise ValueError("file changed during clipboard transfer; copy again")
            else:
                raise ValueError("symbolic links and special files are not supported")
            if output.tell() > FILES_MAX_BYTES:
                raise ValueError("clipboard file archive exceeds 100 MiB")
        for path_string in paths:
            path = Path(path_string)
            add(path, path.name)
    result = output.getvalue()
    if not paths or len(result) > FILES_MAX_BYTES:
        raise ValueError("empty or oversized clipboard file archive")
    return result


def unpack_files(data: bytes, *, directory: str | None = None) -> list[str]:
    """Extract only into a fresh private directory; never overwrite user files."""
    if len(data) > FILES_MAX_BYTES:
        raise ValueError("clipboard file archive exceeds 100 MiB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > MAX_FILES:
            raise ValueError("invalid clipboard file count")
        total = 0
        seen: set[str] = set()
        roots: list[str] = []
        for entry in entries:
            parts = _safe_parts(entry.filename)
            key = "/".join(parts).casefold()
            if key in seen:
                raise ValueError("duplicate archive path")
            seen.add(key)
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("archive contains a link or special file")
            total += entry.file_size
            if total > FILES_MAX_BYTES or entry.flag_bits & 1:
                raise ValueError("oversized or encrypted file archive")
            if parts[0] not in roots:
                roots.append(parts[0])
        folder = Path(tempfile.mkdtemp(prefix="crosskey-clipboard-", dir=directory))
        try:
            for entry in entries:
                target = folder.joinpath(*_safe_parts(entry.filename))
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("xb") as dest:
                        shutil.copyfileobj(source, dest, length=64 * 1024)
        except Exception:
            shutil.rmtree(folder)
            raise
    # Keep staged files after exit so that a later Paste still works.
    return [str(folder / name) for name in roots]
