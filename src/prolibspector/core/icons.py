"""Theme-aware Tk icon loading helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from PIL import Image, ImageTk
    RESAMPLE = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
except ImportError:
    Image = None
    ImageTk = None
    RESAMPLE = None
_ICON_SPEC_ATTR = "_prolibspector_themed_icon_spec"


@dataclass(frozen=True)
class ThemedIconSpec:
    icon_name: str
    size: tuple[int, int]
    reference_attr: str
    owner: Any | None = None
    owner_attr: str | None = None


def themed_icon_path(icon_name: str, theme_name: str | None = None) -> str:
    """Return a theme-appropriate icon path, falling back to the base icon."""
    icon_file_name = Path(icon_name).name
    base_path = Path(resource_path("Icons", icon_file_name))
    theme = (theme_name or current_theme_name()).title()
    if theme == "Dark":
        dark_path = Path(resource_path("Icons", "dark", icon_file_name))
        if dark_path.exists():
            return str(dark_path)
    return str(base_path)


def load_themed_icon(icon_name: str, size: tuple[int, int], theme_name: str | None = None) -> ImageTk.PhotoImage:
    """Load a themed icon as a Tk PhotoImage."""
    image = Image.open(themed_icon_path(icon_name, theme_name)).convert("RGBA")
    return ImageTk.PhotoImage(image.resize(tuple(size), RESAMPLE))


def configure_themed_icon(
    widget: "tk.Widget",
    icon_name: str,
    size: tuple[int, int],
    *,
    owner: Any | None = None,
    owner_attr: str | None = None,
    reference_attr: str = "icon_photo",
) -> ImageTk.PhotoImage:
    """Load, apply, and register an icon so it can refresh after theme changes."""
    photo = load_themed_icon(icon_name, size)
    widget.configure(image=photo)
    setattr(widget, reference_attr, photo)
    if owner is not None and owner_attr:
        setattr(owner, owner_attr, photo)
    setattr(
        widget,
        _ICON_SPEC_ATTR,
        ThemedIconSpec(
            icon_name=icon_name,
            size=tuple(size),
            reference_attr=reference_attr,
            owner=owner,
            owner_attr=owner_attr,
        ),
    )
    return photo


def refresh_themed_icons(root: "tk.Widget") -> None:
    """Reload registered themed icons under a Tk widget tree."""
    for widget in _walk_widgets(root):
        spec = getattr(widget, _ICON_SPEC_ATTR, None)
        if spec is None:
            continue
        try:
            if str(widget.cget("image")) == "":
                continue
            photo = load_themed_icon(spec.icon_name, spec.size)
            widget.configure(image=photo)
            setattr(widget, spec.reference_attr, photo)
            if spec.owner is not None and spec.owner_attr:
                setattr(spec.owner, spec.owner_attr, photo)
        except Exception:
            continue


def _walk_widgets(widget: "tk.Widget"):
    yield widget
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        yield from _walk_widgets(child)
