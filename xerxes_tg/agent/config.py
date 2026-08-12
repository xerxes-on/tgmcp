from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, Field
from xdg_base_dirs import xdg_config_home, xdg_state_home  # type: ignore[import-error]

AGENT_YAML = xdg_config_home() / "xerxes-tg" / "agent.yaml"
STATE_DIR = xdg_state_home() / "xerxes-tg"
AGENT_DB = STATE_DIR / "agent.db"
AGENT_PID = STATE_DIR / "agent.pid"


class ChatConfig(BaseModel):
    id: int
    type: Literal["dm", "group"]
    tone: Literal["professional", "friendly"] = "friendly"


class AgentConfig(BaseModel):
    confidence_threshold: float = Field(0.80, ge=0.0, le=1.0)
    debounce_seconds: float = Field(4.0, gt=0.0)
    approval_ttl_minutes: int = Field(60, gt=0)
    owned_services: list[str] = Field(default_factory=list)
    chats: list[ChatConfig] = Field(default_factory=list)
    orchestrator_model: str = "claude-sonnet-4-6"
    summarizer_model: str = "claude-haiku-4-5-20251001"
    max_history_messages: int = 50

    @classmethod
    def load(cls) -> AgentConfig:
        if not AGENT_YAML.exists():
            return cls()
        data = yaml.safe_load(AGENT_YAML.read_text()) or {}
        return cls.model_validate(data)

    def chat_config(self, chat_id: int) -> ChatConfig | None:
        for c in self.chats:
            if c.id == chat_id:
                return c
        return None

    def is_watched(self, chat_id: int) -> bool:
        return any(c.id == chat_id for c in self.chats)

    def write_example(self) -> None:
        AGENT_YAML.parent.mkdir(parents=True, exist_ok=True)
        example = {
            "confidence_threshold": 0.80,
            "debounce_seconds": 4.0,
            "approval_ttl_minutes": 60,
            "owned_services": ["ufarm-api", "ufarm-market", "ufarm-billing", "ufarm-auth", "ufarm-notifications"],
            "orchestrator_model": "claude-sonnet-4-6",
            "summarizer_model": "claude-haiku-4-5-20251001",
            "max_history_messages": 50,
            "chats": [
                {"id": -1001234567890, "type": "group", "tone": "professional"},
                {"id": 1234567890, "type": "dm", "tone": "friendly"},
            ],
        }
        AGENT_YAML.write_text(yaml.safe_dump(example, sort_keys=False, default_flow_style=False))
