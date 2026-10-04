def previous_tag(current: str, tags_newest_first: list[str]) -> str | None:
    """Tag released before `current`, given `git tag --sort=-v:refname` output."""
    tags = [t.strip() for t in tags_newest_first if t.strip()]
    if current not in tags:
        raise ValueError(f"tag {current} not found")
    idx = tags.index(current)
    return tags[idx + 1] if idx + 1 < len(tags) else None


def version_name(tag: str) -> str:
    return tag[1:] if tag[:1] in ("v", "V") else tag
