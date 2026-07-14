"""Avalanche flow simulation: where does the snow go once it releases?

Two engines, both deliberately transparent:

* :mod:`runout` -- fast interactive routing, and an advanced particle ensemble.
* :mod:`exposure` -- what the simulated flow hits.

Both are behind :class:`runout.RunoutEngine`, so a validated external engine
(RAMMS, SamosAT, ...) can be dropped in without touching anything upstream.

Neither engine has been validated against an observed Mount Hosmer avalanche,
because none are recorded. The alpha angles come from published Canadian Rockies
ranges, not from local back-analysis. A runout line drawn with false precision is
worse than no line at all, so every result carries an explicit uncertainty
envelope.
"""
