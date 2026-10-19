"""Shared slide groups and account-private file directories."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import HTTPException, Request

from .auth_store import AuthStore
from .registry import SlideRegistry


def namespace(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    # Flat legacy uploads and crops belong to the administrator.
    return user["id"] if user and not user["is_admin"] else None


def private_directory(root: Path, owner: str | None) -> Path:
    directory = root if owner is None else root / owner
    if owner is not None and (
        directory.is_symlink() or not directory.resolve().is_relative_to(root)
    ):
        raise HTTPException(404, "unknown file directory")
    return directory


class FileAccess:
    def __init__(
        self,
        store: AuthStore,
        registry: SlideRegistry,
        roots: tuple[Path, ...],
        upload_root: Path,
        crop_root: Path,
    ):
        self.store, self.registry = store, registry
        self.upload_root = upload_root
        self.private_roots = (upload_root, crop_root)
        self.roots = tuple(sorted(roots, key=lambda p: len(p.parts), reverse=True))
        self.refresh_groups()

    @staticmethod
    def directory_key(root: Path, directory: Path) -> str:
        stat = directory.stat()
        return hashlib.sha256(
            f"{root}:{stat.st_dev}:{stat.st_ino}".encode()
        ).hexdigest()

    def refresh_groups(self) -> list[dict]:
        entries = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for directory in sorted(root.iterdir()):
                if (
                    directory.name.startswith(".")
                    or directory.is_symlink()
                    or not directory.is_dir()
                ):
                    continue
                name = (
                    directory.name
                    if len(self.roots) == 1
                    else f"{root.name}/{directory.name}"
                )
                try:
                    entries.append((self.directory_key(root, directory), name))
                except OSError:
                    continue
        return self.store.sync_groups(entries)

    def allowed_slide(self, user: dict, slide_id: str) -> bool:
        if slide_id not in self.registry:
            return False
        uploaded = self.store.uploaded(slide_id)
        if uploaded:
            if uploaded["user_id"] != user["id"]:
                return False
            directory = self.upload_root / user["id"]
            path = directory / uploaded["filename"]
            try:
                return (
                    not directory.is_symlink()
                    and not path.is_symlink()
                    and self.registry[slide_id]
                    .path.resolve(strict=True)
                    .is_relative_to(directory)
                )
            except (OSError, RuntimeError):
                return False
        try:
            source = self.registry[slide_id].path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        for private_root in self.private_roots:
            if source.is_relative_to(private_root):
                return bool(user["is_admin"] and source.parent == private_root)
        if user["is_admin"]:
            return True
        for root in self.roots:
            try:
                relative = source.relative_to(root)
            except ValueError:
                continue
            if len(relative.parts) < 2 or relative.parts[0].startswith("."):
                return False
            directory = root / relative.parts[0]
            if directory.is_symlink() or not directory.is_dir():
                return False
            try:
                group_id = self.store.group_id(self.directory_key(root, directory))
            except OSError:
                return False
            return group_id is not None and group_id in self.store.user_groups(
                user["id"]
            )
        return False

    def allowed_request(self, request: Request, user: dict) -> bool:
        path = request.url.path
        if path.startswith("/api/slides/") and path != "/api/slides/upload":
            return self.allowed_slide(user, path.split("/")[3])
        if path.startswith("/iiif/3/"):
            return self.allowed_slide(user, path.split("/")[3])
        return True
