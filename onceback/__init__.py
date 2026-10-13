"""OnceBack: every refund goes back once, in the right currency, on the right rail.

An agent for cross-border merchants on Airwallex. The model reads customer
messages; code decides amounts, rails, approvals and duplicate protection;
Airwallex APIs move the money (card refund, or bank payout with account
verification and FX when the card rail is closed).
"""
__version__ = "0.1.0"
