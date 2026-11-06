"""
GPU scheduler.

The policy modules (eligibility, classes, scoring) are pure functions of their
inputs and an injected clock, so they are tested without a database
(NFR-GPU-054). Persistence and the claim transaction live in their own modules.
"""
