"""Marginalia: talk to a book's characters, who know only what you have read so far.

The model has a small context, so nothing lasting lives in a prompt. Everything is kept in memory.py, and each
question is answered from a small slice of it, chosen under a fixed budget (ask.py). The lock is applied in the
database: text past your place is never selected, so it cannot reach the model. Standard library only, and bookread
works the same with this turned off or the model down.
"""
