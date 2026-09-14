"""Wire types for channel-defined task fields and their attachment values."""

from typing import Literal, NotRequired, TypedDict

TrackerFieldType = Literal[
    "text",
    "textarea",
    "select",
    "multiselect",
    "number",
    "date",
    "checkbox",
    "url",
    "users",
    "channels",
    "attachments",
]


class TrackerField(TypedDict):
    id: str
    name: str
    type: TrackerFieldType
    options: NotRequired[list[str]]


class TrackerUploadedAttachment(TypedDict):
    id: str
    name: str
    type: Literal["file", "image", "video"]


class TrackerLinkedAttachment(TypedDict):
    url: str
    name: str
    type: Literal["file", "image", "video"]
