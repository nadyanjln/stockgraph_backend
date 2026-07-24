"""Backward-compatible facade for StockGraph agents.

New code should import from the specific modules:
- app.core.agent.news_agent
- app.core.agent.financial_agent
- app.core.agent.manager_agent
- app.core.agent.common
"""

from app.core.agent.common import (
    FINANCIAL_MODEL,
    MANAGER_MODEL,
    NEWS_MODEL,
    AgentContext,
    AgentName,
    ChatMessage,
    chat_complete,
    stream_chat,
    trim_history,
)
from app.core.agent.financial_agent import (
    FINANCIAL_SYSTEM,
    retrieve_financial_context,
    run_financial_agent,
)
from app.core.agent.manager_agent import (
    MANAGER_ROUTER_SYSTEM,
    MANAGER_SYNTHESIZER_SYSTEM,
    ManagerPlan,
    manager_plan,
    manager_synthesizer_messages,
    stream_synthesis,
    synthesize_answer,
)
from app.core.agent.news_agent import NEWS_SYSTEM, retrieve_news_context, run_news_agent

__all__ = [
    "AgentContext",
    "AgentName",
    "ChatMessage",
    "NEWS_MODEL",
    "FINANCIAL_MODEL",
    "MANAGER_MODEL",
    "chat_complete",
    "stream_chat",
    "trim_history",
    "NEWS_SYSTEM",
    "retrieve_news_context",
    "run_news_agent",
    "FINANCIAL_SYSTEM",
    "retrieve_financial_context",
    "run_financial_agent",
    "MANAGER_ROUTER_SYSTEM",
    "MANAGER_SYNTHESIZER_SYSTEM",
    "ManagerPlan",
    "manager_plan",
    "manager_synthesizer_messages",
    "synthesize_answer",
    "stream_synthesis",
]
