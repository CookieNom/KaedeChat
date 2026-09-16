from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import Field, model_validator

from app.chat.schemas import RequestModel
from app.core.permissions import Permission


class OnboardingOption(RequestModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)
    emoji: str = Field(default="", max_length=16)
    role_ids: list[str] = Field(default_factory=list, max_length=20)
    channel_ids: list[str] = Field(default_factory=list, max_length=50)


class OnboardingQuestion(RequestModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    required: bool = False
    multiple: bool = False
    before_join: bool = True
    options: list[OnboardingOption] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_options(self) -> OnboardingQuestion:
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("Answer IDs must be unique within a question")
        return self


class GuideItem(RequestModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)
    channel_id: str | None = None
    kind: Literal["task", "resource"] = "task"


class OnboardingConfig(RequestModel):
    enabled: bool = False
    revision: int = Field(default=0, ge=0)
    rules_revision: int = Field(default=0, ge=0)
    welcome: str = Field(default="Welcome! Make yourself at home.", max_length=1000)
    rules: list[str] = Field(default_factory=list, max_length=16)
    default_channel_ids: list[str] = Field(default_factory=list, max_length=100)
    questions: list[OnboardingQuestion] = Field(default_factory=list, max_length=12)
    guide: list[GuideItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_content(self) -> OnboardingConfig:
        if any(not rule.strip() or len(rule) > 1000 for rule in self.rules):
            raise ValueError("Rules must contain between 1 and 1000 characters")
        for items in (self.questions, self.guide):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Question and guide IDs must be unique")
        if self.enabled and not (self.rules or self.questions or self.guide):
            raise ValueError("Add rules, questions, or a server guide before enabling onboarding")
        return self


class OnboardingAnswers(RequestModel):
    revision: int = Field(ge=0)
    accept_rules: bool = False
    answers: dict[str, list[str]] = Field(default_factory=dict, max_length=12)
    completed_tasks: list[str] = Field(default_factory=list, max_length=20)
    extra_channel_ids: list[str] = Field(default_factory=list, max_length=100)


# Self-selected roles may grant participation, never moderation or administration.
SELF_ASSIGNABLE_PERMISSIONS = int(
    Permission.VIEW_CHANNEL
    | Permission.READ_MESSAGE_HISTORY
    | Permission.SEND_MESSAGES
    | Permission.ADD_REACTIONS
    | Permission.CONNECT
    | Permission.SPEAK
    | Permission.STREAM
    | Permission.USE_VAD
    | Permission.ATTACH_FILES
    | Permission.EMBED_LINKS
    | Permission.SEND_MESSAGES_IN_THREADS
)


def needs_rules(config: dict[str, Any] | None, state: dict[str, Any] | None) -> bool:
    config, state = config or {}, state or {}
    return bool(
        config.get("enabled")
        and config.get("rules")
        and state.get("rules_revision") != config.get("rules_revision")
    )


def selected_onboarding(
    config: OnboardingConfig, payload: OnboardingAnswers
) -> tuple[set[str], set[str]]:
    if payload.revision != config.revision:
        raise HTTPException(
            409,
            detail={
                "code": "ONBOARDING_CHANGED",
                "message": "The server setup changed. Reload and review it again.",
            },
        )
    questions = {question.id: question for question in config.questions}
    if set(payload.answers) - questions.keys():
        raise HTTPException(422, detail={"code": "ONBOARDING_INVALID_ANSWER"})
    roles: set[str] = set()
    channels = set(config.default_channel_ids)
    for question in config.questions:
        answers = payload.answers.get(question.id, [])
        options = {option.id: option for option in question.options}
        if (
            len(answers) != len(set(answers))
            or set(answers) - options.keys()
            or (not question.multiple and len(answers) > 1)
            or (question.required and question.before_join and not answers)
        ):
            raise HTTPException(
                422,
                detail={
                    "code": "ONBOARDING_INVALID_ANSWER",
                    "message": f"Check your answer to {question.title}.",
                },
            )
        for answer in answers:
            roles.update(options[answer].role_ids)
            channels.update(options[answer].channel_ids)
    if set(payload.completed_tasks) - {item.id for item in config.guide if item.kind == "task"}:
        raise HTTPException(422, detail={"code": "ONBOARDING_INVALID_TASK"})
    return roles, channels
