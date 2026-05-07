"""AurumDesk negotiation environment — Verifiers wrapper for Prime Intellect.

Wraps the existing two-agent negotiation bench (tasks.json) so it can be driven
by Verifiers' rollout/rubric protocol. Step 1 of Prime Intellect compatibility:
instance source is hardcoded test rows from tasks.json; will swap to a procedural
sampler in a follow-up step without changing the Verifiers contract.
"""

from aurumdesk_env.env import AurumDeskNegotiationEnv, load_environment

__all__ = ["AurumDeskNegotiationEnv", "load_environment"]
