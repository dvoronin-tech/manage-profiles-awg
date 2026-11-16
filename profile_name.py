PROFILE_NAME_RULE = "Use a printable profile name without slashes or leading or trailing spaces (120 bytes maximum)."


def valid_profile_name(name):
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and name == name.strip()
        and name.isprintable()
        and "/" not in name
        and "\\" not in name
        and len(name.encode("utf-8")) <= 120
    )
