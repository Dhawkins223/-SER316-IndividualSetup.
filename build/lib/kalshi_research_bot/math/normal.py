"""Standard normal CDF and quantile, with no third-party dependency.

Kept in ``math`` rather than in ``evaluation`` because both the power
calculations and the copula in ``slip_analysis`` need them, and ``evaluation``
reaches into the rest of the package -- importing it from ``slip_analysis``
closed an import cycle. These are pure functions of a float and depend on
nothing in this project, so a leaf module is where they belong.

Acklam's rational approximation gives about nine significant figures; one
Halley refinement against ``erfc`` takes it to full double precision, which
matters because these quantiles are squared in every sample-size formula and
used as copula thresholds.
"""

from __future__ import annotations

from math import erf, erfc, exp, log, pi, sqrt

_ACKLAM_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_ACKLAM_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_LOW = 0.02425


def normal_cdf(value: float) -> float:
    """Standard normal cumulative distribution."""

    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def normal_quantile(probability: float) -> float:
    """Inverse standard normal CDF.

    Acklam's approximation gives about nine significant figures; one Halley
    refinement against ``erfc`` takes it to full double precision, which matters
    because these quantiles are squared in every sample-size formula below.
    """

    if not 0.0 < probability < 1.0:
        raise ValueError("normal_quantile_requires_probability_strictly_between_zero_and_one")
    if probability < _LOW:
        q = sqrt(-2.0 * log(probability))
        estimate = (((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / (
            (((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0
        )
    elif probability <= 1.0 - _LOW:
        q = probability - 0.5
        r = q * q
        estimate = (((((_ACKLAM_A[0] * r + _ACKLAM_A[1]) * r + _ACKLAM_A[2]) * r + _ACKLAM_A[3]) * r + _ACKLAM_A[4]) * r + _ACKLAM_A[5]) * q / (
            ((((_ACKLAM_B[0] * r + _ACKLAM_B[1]) * r + _ACKLAM_B[2]) * r + _ACKLAM_B[3]) * r + _ACKLAM_B[4]) * r + 1.0
        )
    else:
        q = sqrt(-2.0 * log(1.0 - probability))
        estimate = -(((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / (
            (((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0
        )
    error = 0.5 * erfc(-estimate / sqrt(2.0)) - probability
    density = exp(-estimate * estimate / 2.0) / sqrt(2.0 * pi)
    if density > 0:
        step = error / density
        estimate -= step / (1.0 + estimate * step / 2.0)
    return estimate
