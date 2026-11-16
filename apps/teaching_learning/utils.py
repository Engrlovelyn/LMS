"""Formatting helpers shared by the teaching & learning surfaces.

Learning materials are shown on five different screens (teacher classroom,
student classroom, the Academic Coordinator / Principal / System Administrator
repositories and the student dashboard). Every one of them has to label a file
the same way, so the conversion lives here instead of being copy-pasted into
each payload builder.
"""


def human_size(num):
    """Return a file size as ``"512 B"`` / ``"1.5 KB"`` / ``"2.4 MB"``.

    Values are binary (1 KB = 1024 B) because that is what both the operating
    system and the browsers report for an uploaded file, so the size a teacher
    sees on their machine matches the size the dashboard shows.

    An unusable value (``None``, a string, a storage backend that cannot report
    a size) returns ``""`` rather than ``"0 B"``: a size we do not know must not
    be displayed as a size we do know.
    """
    try:
        num = float(num)
    except (TypeError, ValueError):
        return ""
    if num < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            if unit == "B":
                return "%d %s" % (num, unit)
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return ""


def resource_size_label(resource):
    """``"2.4 MB"`` for a stored resource, ``""`` when it has no file.

    ``resource.file.size`` reaches the storage backend, which can fail (a
    missing object, an unreachable bucket). A material row must still render in
    that case, so the failure degrades to "no size label" instead of a 500.
    """
    size = resource_size_bytes(resource)
    return "" if size is None else human_size(size)


def resource_size_bytes(resource):
    """The stored file's size in bytes, or ``None`` when it cannot be read."""
    file_field = getattr(resource, "file", None)
    if not file_field:
        return None
    try:
        return file_field.size
    except Exception:
        return None
