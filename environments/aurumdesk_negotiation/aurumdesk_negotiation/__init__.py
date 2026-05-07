"""AurumDesk negotiation environment for Verifiers / Prime Intellect.

Wraps a synthetic B2B precious-metals brokerage scenario as a two-agent
negotiation environment. The seller (the model under test) follows a written
policy, uses tools to look up customers, prices, and policies, and negotiates
against an adversarial buyer-side LLM. The reward signal is a weighted blend
of policy compliance (no below-floor quotes, no secret leakage), correct DB
state (the right quotes/orders are written), and partial credit on captured
ZOPA surplus for negotiation tasks.

Entry point:
    >>> from aurumdesk_negotiation import load_environment
    >>> env = load_environment()
    >>> # then drive via verifiers' rollout/rubric protocol
"""

from .env import AurumDeskNegotiationEnv, load_environment

__all__ = ["AurumDeskNegotiationEnv", "load_environment"]
