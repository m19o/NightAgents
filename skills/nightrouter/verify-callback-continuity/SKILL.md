# Verify Callback Continuity

Version: 1
Agent: NightRouter

## Purpose

Prove expected callbacks arrived through the replacement route before requesting an old-route drain.

## Rules

- Account for each callback's expected check-in interval.
- Unavailable Mythic state is unknown, not zero callbacks.
- Missing callbacks prohibit drain and retirement.
- Emit evidence suitable for Safety Kernel validation.

