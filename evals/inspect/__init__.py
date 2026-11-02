"""Bespoke prompt-quality evaluation harness.

This package sits alongside ``evals/`` (scripted-provider case regression,
which measures the program) and asks whether the current prompt suite classifies
each requirement the way independent blinded reviewer agents labeled it.
Workspace rulings are calibration evidence, never gold labels.

The harness runs offline by default. Its real-provider solver is available only
behind the explicit ``scripts/eval.sh --live`` approval and spend boundary.
"""
