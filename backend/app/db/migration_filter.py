from __future__ import annotations

import re

MESSAGE_PARTITION_RE = re.compile(r"^messages_\d{4}_\d{2}$")


def is_message_partition(name: str) -> bool:
    return MESSAGE_PARTITION_RE.fullmatch(name) is not None


def references_message_partition(constraint: object) -> bool:
    for element in getattr(constraint, "elements", ()):
        target = getattr(element, "target_fullname", "")
        target_table = target.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        if is_message_partition(target_table):
            return True
    return False
