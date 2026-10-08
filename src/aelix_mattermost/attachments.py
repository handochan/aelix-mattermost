"""Files users attach (described to the model, saved for tools) and files Aelix sends back."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import shutil
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .mattermost import FileTooLarge, MattermostClient, MattermostError

log = logging.getLogger(__name__)

INBOX = "attachments"
OUTBOX = "outbox"
FILES_PER_POST = 10  # Mattermost's limit (PostFileidsMaxRunes)
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # larger images are saved but not sent to the model
MAX_IMAGES_BYTES = 20 * 1024 * 1024  # images sent with one message, in total
INBOX_QUOTA = 200 * 1024 * 1024  # saved attachments per conversation; the oldest posts go first
OUTBOX_MAX_FILES = 1000  # files looked at in an outbox
OUTBOX_MAX_DEPTH = 4  # directory levels below outbox/
NAME_BYTES = 180  # a file name in UTF-8, leaving room for "-NNN" within the usual 255
# The magic numbers of the image types every vision provider accepts.
_IMAGES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}
_TEXT_TYPES = {
    "application/json", "application/xml", "application/x-yaml", "application/yaml", "application/toml",
    "application/javascript", "application/x-sh", "application/sql", "application/x-ndjson",
}
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx",
    ".jsx", ".py", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb",
    ".php", ".swift", ".sh", ".bash", ".zsh", ".sql", ".log", ".diff", ".patch", ".tex", ".svg",
}
_UNSAFE = re.compile(r"[^\w.\- ()\[\]+=,@]")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass
class Attachment:
    name: str
    path: Path | None  # relative to the session workspace; None when not saved (no tools)
    mime: str
    size: int
    kind: str  # "image", "text" or "file"
    text: str | None = None
    truncated: bool = False
    image: dict | None = None  # an Aelix RPC ImageContent
    too_large: bool = False  # an image over the per-image or per-message budget


@dataclass
class Inbound:
    attachments: list[Attachment] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # why some files were left out (Korean, for users)

    @property
    def images(self) -> list[dict]:
        return [x.image for x in self.attachments if x.image is not None]


def safe_name(name: str, fallback: str) -> str:
    """A file name that stays inside its directory and is readable in a prompt."""
    name = unicodedata.normalize("NFC", Path(name.replace("\\", "/")).name)
    name = "".join(c for c in name if c.isprintable())
    name = _UNSAFE.sub("_", name)
    name = name.encode("utf-8")[:NAME_BYTES].decode("utf-8", "ignore").strip(" .")
    return name or fallback


def _real_directory(root: Path, *parts: str) -> Path:
    """root/parts..., created as real directories: tools may have planted a symlink there."""
    path = root
    for part in parts:
        path = path / part
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError(f"{path} is not a directory")
    return path


def _write_new(directory: Path, name: str, data: bytes) -> Path:
    """Write data to a new file named like `name`, never following or replacing anything."""
    stem, suffix = Path(name).stem, Path(name).suffix
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_BINARY", 0)
    for number in range(1, 1000):
        path = directory / (name if number == 1 else f"{stem}-{number}{suffix}")
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return path
    raise OSError("too many files with the same name")


def _tree_size(path: Path) -> int:
    total = 0
    for directory, _, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                total += os.lstat(Path(directory) / name).st_size
            except OSError:
                pass
    return total


def _make_room(inbox: Path, incoming: int) -> None:
    """Remove the oldest saved posts until `incoming` more bytes fit in INBOX_QUOTA."""
    try:
        posts = [x for x in inbox.iterdir() if stat.S_ISDIR(os.lstat(x).st_mode)]
    except OSError:
        return
    sizes = {x: _tree_size(x) for x in posts}
    total = sum(sizes.values())
    for post in sorted(posts, key=lambda x: os.lstat(x).st_mtime):
        if total + incoming <= INBOX_QUOTA:
            break
        shutil.rmtree(post, ignore_errors=True)
        total -= sizes[post]


def _mime(info: dict, name: str) -> str:
    mime = info.get("mime_type")
    if isinstance(mime, str) and mime:
        return mime.split(";")[0].strip().lower()
    return (mimetypes.guess_type(name)[0] or "application/octet-stream").lower()


def _is_image(mime: str, data: bytes) -> bool:
    signatures = _IMAGES.get(mime)
    if not signatures or not data.startswith(signatures):
        return False
    return mime != "image/webp" or data[8:12] == b"WEBP"


def _is_text(mime: str, name: str) -> bool:
    return (mime.startswith("text/") or mime in _TEXT_TYPES or mime.endswith(("+json", "+xml"))
            or Path(name).suffix.lower() in _TEXT_SUFFIXES)


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _megabytes(size: int) -> str:
    """A size for people: "20MB", "1.5MB", "800KB"."""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}".removesuffix(".0") + "MB"
    return f"{max(1, round(size / 1024))}KB"


async def fetch(client: MattermostClient, config: Config, post_id: str, file_ids: tuple[str, ...],
                files: tuple[dict, ...], workspace: Path, vision: bool, save: bool) -> Inbound:
    """Download a post's files and describe them to the model.

    UTF-8 text files are inlined up to max_inline_text_chars in total; images go to the
    model as images when it reads them (`vision`) and they fit MAX_IMAGE_BYTES and
    MAX_IMAGES_BYTES. With `save` (the conversation has tools), every file is also saved
    under workspace/attachments/<post id>/ for tools to open."""
    result = Inbound()
    if not file_ids:
        return result
    known = {x.get("id"): x for x in files if isinstance(x.get("id"), str)}
    if config.max_attachments == 0:
        result.skipped.append(f"첨부파일 {len(file_ids)}개는 이 게이트웨이에서 받지 않도록 설정되어 있습니다.")
        return result
    if len(file_ids) > config.max_attachments:
        result.skipped.append(f"첨부파일은 {config.max_attachments}개까지만 전달됩니다 "
                              f"({len(file_ids) - config.max_attachments}개 제외).")
    directory: Path | None = None
    if save:
        try:
            directory = await asyncio.to_thread(_real_directory, workspace, INBOX, safe_name(post_id, "post"))
        except OSError as exc:
            log.warning("Cannot store attachments post=%s (%s)", post_id[:8], type(exc).__name__)
            result.skipped.append("첨부파일을 저장할 수 없어 저장하지 않았습니다.")
    text_budget, image_budget = config.max_inline_text_chars, MAX_IMAGES_BYTES
    for index, file_id in enumerate(file_ids[:config.max_attachments]):
        info, name = known.get(file_id), f"file-{index + 1}"
        try:
            if info is None:
                info = await client.file_info(file_id)
            name = safe_name(str(info.get("name") or ""), name)
            size = info.get("size")
            if isinstance(size, int) and size > config.max_attachment_bytes:
                raise FileTooLarge("declared size")
            data = await client.download(file_id, config.max_attachment_bytes)
        except FileTooLarge:
            result.skipped.append(f"`{name}`: {_megabytes(config.max_attachment_bytes)}보다 커서 제외했습니다.")
            continue
        except MattermostError as exc:
            log.warning("Could not download an attachment post=%s (%s)", post_id[:8], type(exc).__name__)
            result.skipped.append(f"`{name}`을(를) 내려받지 못했습니다.")
            continue
        path = None
        if directory is not None:
            try:
                await asyncio.to_thread(_make_room, directory.parent, len(data))
                path = (await asyncio.to_thread(_write_new, directory, name, data)).relative_to(workspace)
            except OSError as exc:
                log.warning("Cannot store an attachment post=%s (%s)", post_id[:8], type(exc).__name__)
        mime = _mime(info, name)
        attachment = Attachment(name, path, mime, len(data), "file")
        if _is_image(mime, data):
            attachment.kind = "image"
            if vision and len(data) <= min(MAX_IMAGE_BYTES, image_budget):
                attachment.image = {"type": "image", "mimeType": mime, "data": base64.b64encode(data).decode()}
                image_budget -= len(data)
            elif vision:
                attachment.too_large = True
        elif _is_text(mime, name) and (text := _decode(data)) is not None:
            attachment.kind = "text"
            if text_budget > 0:
                attachment.truncated = len(text) > text_budget
                attachment.text = text[:text_budget]
                text_budget -= len(attachment.text)
        result.attachments.append(attachment)
        del data
    return result


def purge(workspace: Path) -> None:
    """Remove a conversation's saved attachments and unsent outbox files (`!new`)."""
    for name in (INBOX, OUTBOX):
        path = workspace / name
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except OSError:
                pass


# -- outgoing files -------------------------------------------------------------------


async def outbox_snapshot(workspace: Path) -> dict[str, tuple[int, int]]:
    """(mtime_ns, size) of each regular file in the outbox, so only new files are sent."""
    return {str(path): stamp for path, stamp in await asyncio.to_thread(_outbox_files, workspace)}


def _outbox_files(workspace: Path) -> list[tuple[Path, tuple[int, int]]]:
    """Regular files under outbox/ (no symlinks or special files, no hidden directories),
    at most OUTBOX_MAX_FILES of them, at most OUTBOX_MAX_DEPTH levels deep."""
    root = workspace / OUTBOX
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):  # a symlinked outbox could reach anywhere
            return []
    except OSError:
        return []
    found: list[tuple[Path, tuple[int, int]]] = []
    seen = 0
    for directory, folders, names in os.walk(root, followlinks=False):
        depth = len(Path(directory).relative_to(root).parts)
        folders[:] = [] if depth >= OUTBOX_MAX_DEPTH else sorted(x for x in folders if not x.startswith("."))
        for name in sorted(names):
            seen += 1
            if seen > OUTBOX_MAX_FILES:
                return found
            path = Path(directory) / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):  # never a symlink, device or FIFO
                found.append((path, (info.st_mtime_ns, info.st_size)))
    return found


@dataclass
class Outgoing:
    files: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


async def outbox_changes(workspace: Path, before: dict[str, tuple[int, int]], config: Config) -> Outgoing:
    """Files written to the outbox since `before`, within the upload limits."""
    result = Outgoing()
    for path, stamp in await asyncio.to_thread(_outbox_files, workspace):
        if before.get(str(path)) == stamp:
            continue
        if stamp[1] > config.max_upload_bytes:
            result.skipped.append(f"`{path.name}`: {_megabytes(config.max_upload_bytes)}보다 커서 보내지 못했습니다.")
        elif stamp[1] == 0:
            continue
        else:
            result.files.append(path)
    limit = FILES_PER_POST * 3
    if len(result.files) > limit:
        result.skipped.append(f"파일은 한 번에 {limit}개까지 보냅니다 ({len(result.files) - limit}개 제외).")
        result.files = result.files[:limit]
    return result


_DIRECTORY_FDS = os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")


def _open_outbox_file(path: Path, root: Path) -> tuple[int | None, int]:
    """(descriptor of the file's directory, descriptor of the file) for an outbox file.

    Every directory from outbox/ down is opened without following symlinks and the file is
    opened relative to its directory, so a directory swapped for a symlink after the outbox
    was listed cannot redirect the read (or the later unlink) elsewhere. Where descriptors
    cannot be used (Windows), the resolved parent is checked instead."""
    flags = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    parts = path.relative_to(root).parts
    if not _DIRECTORY_FDS:
        if not path.parent.resolve().is_relative_to(root.resolve()):
            raise OSError("outside the outbox")
        return None, os.open(path, flags)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory, os.open(parts[-1], flags, dir_fd=directory)
    except BaseException:
        os.close(directory)
        raise


def _read_outbox_file(path: Path, root: Path, limit: int) -> tuple[int | None, bytes]:
    """(directory descriptor for _remove_sent, bytes) of a regular file: no symlink, FIFO or
    device, at most `limit` bytes."""
    directory, descriptor = _open_outbox_file(path, root)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise OSError("not a regular file within the limit")
        chunks, total = [], 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise OSError("grew past the limit")
            chunks.append(chunk)
        return directory, b"".join(chunks)
    except BaseException:
        if directory is not None:
            os.close(directory)
        raise
    finally:
        os.close(descriptor)


def _remove_sent(path: Path, root: Path, directory: int | None) -> None:
    """Remove a sent file through the directory it was read from."""
    try:
        if directory is not None:
            os.unlink(path.name, dir_fd=directory)
        elif path.parent.resolve().is_relative_to(root.resolve()):
            path.unlink()
    except OSError:
        pass
    finally:
        if directory is not None:
            os.close(directory)


async def upload(client: MattermostClient, channel_id: str, workspace: Path, files: list[Path],
                 config: Config) -> tuple[list[str], list[str]]:
    """Upload outbox files; returns (file ids, notes about files that failed). Sent files are removed."""
    ids, notes = [], []
    root = workspace / OUTBOX
    for path in files:
        directory = None
        try:
            directory, data = await asyncio.to_thread(_read_outbox_file, path, root, config.max_upload_bytes)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            ids.append(await client.upload(channel_id, safe_name(path.name, "file"), data, mime))
        except (OSError, ValueError, MattermostError) as exc:
            log.warning("Could not upload an outbox file (%s)", type(exc).__name__)
            notes.append(f"`{safe_name(path.name, 'file')}`을(를) 보내지 못했습니다.")
            if directory is not None:
                os.close(directory)
            continue
        await asyncio.to_thread(_remove_sent, path, root, directory)
    return ids, notes
