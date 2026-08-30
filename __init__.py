"""Directory-plugin entry point for Hermes Harness Control."""

if __package__:
    from .harness_control.plugin import register
else:
    from harness_control.plugin import register

__all__ = ["register"]
