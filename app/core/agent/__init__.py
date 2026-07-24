"""StockGraph agent package."""

from app.core.agent.common import AgentContext, trim_history
from app.core.agent.financial_agent import run_financial_agent
from app.core.agent.manager_agent import (
    manager_plan,
    manager_synthesizer_messages,
    stream_synthesis,
    synthesize_answer,
)
from app.core.agent.news_agent import run_news_agent
from app.core.agent.orchestrator import HISTORY_TURNS, Orchestrator, Session, SessionStore

__all__ = [
    "Orchestrator",
    "Session",
    "SessionStore",
    "HISTORY_TURNS",
    "AgentContext",
    "manager_plan",
    "manager_synthesizer_messages",
    "synthesize_answer",
    "stream_synthesis",
    "run_news_agent",
    "run_financial_agent",
    "trim_history",
]
