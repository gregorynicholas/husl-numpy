"""
HUSL (human-friendly Hue, Saturation, and Lightness)
color conversion. Found in this module:

1. HUSL <-> RGB color conversion API
   a. `to_husl`: converts an RGB array to a HUSL array
   b. `to_rgb`: converts a HUSL array to and RGB array
   c. `to_hue`: converts an RGB array to an array of HUSL hue values
2. The NumPy implementation of these conversions. Functions with
   alternative implementations in C, Cython, or NumExpr
   are flagged with the `@optimized` decorator, and they can be enabled with
   the context managers (e.g. `with simd_enabled: ...`) in `__init__`.
   By default, C functions are used if they're available.
"""

import math
import warnings

import numpy as np

from numpy import ndarray
from . import constants
from . import transform


### The API
### From RGB: to_husl, to_hue
### From HUSL: to_rgb

@transform.squeeze_output
@transform.reshape_image_input
@transform.reshape_rgba_input
def to_hue(rgb_img: ndarray, chunksize: int = None,
           out: ndarray = None) -> ndarray:
    """Convert an RGB image of integers to a 2D array of HUSL hues"""
    return transform.in_chunks(rgb_img, _rgb_to_hue, chunksize, out)


@transform.squeeze_output
@transform.reshape_husl_input
@transform.rgb_int_output
def to_rgb(husl_img: ndarray, chunksize: int = None,
           out: ndarray = None) -> ndarray:
    """Convert a 3D HUSL array of floats to a 3D RGB array of integers"""
    return transform.in_chunks(husl_img, _husl_to_rgb, chunksize, out)


@transform.squeeze_output
@transform.reshape_image_input
@transform.reshape_rgba_input
def to_husl(rgb_img: ndarray, chunksize: int = None,
            out: ndarray = None) -> ndarray:
    """Convert an RGB image of integers to a 3D array of HSL values"""
    return transform.in_chunks(rgb_img, _rgb_to_husl, chunksize, out)


### Optimization selection

try:
    from . import _numexpr_opt as expr
except ImportError:
    expr = None
try:
    from . import _cython_opt as cyth
except ImportError as e:
    warnings.warn("No Cython extension module: {}".format(e))
    cyth = None
try:
    from . import _simd_opt as simd
except ImportError as e:
    warnings.warn("No SIMD extension module: {}".format(e))
    simd = None


_NUMEXPR_ENABLED = True
_CYTHON_ENABLED = True
_SIMD_ENABLED = True
NUMPY = {}  # normal cpython/numpy fns
NUMEXPR = {}  # numexpr fns
CYTHON = {}  # cython extension fns
SIMD = {}  # cython-wrapped C SIMD parallelization


def optimized(fn):
    """Decorator for functions with multiple implementations.
    Registers the function in optimization dictionaries and chooses
    the fastest available implementation. Alternate implementations can
    be enabled by the user at runtime (e.g. for unit testing)."""
    NUMPY[fn.__name__] = fn
    expr_fn = getattr(expr, fn.__name__, None) if _NUMEXPR_ENABLED else None
    cython_fn = getattr(cyth, fn.__name__, None) if _CYTHON_ENABLED else None
    simd_fn = getattr(simd, fn.__name__, None) if _SIMD_ENABLED else None
    opt_fn = simd_fn or cython_fn or expr_fn  # prefer SIMD
    if simd_fn:
        SIMD[fn.__name__] = simd_fn
    if cython_fn:
        CYTHON[fn.__name__] = cython_fn
    if expr_fn:
        NUMEXPR[fn.__name__] = expr_fn
    result_fn = opt_fn or fn
    return result_fn


### Conversions in the direction of RGB -> HUSL

L_MAX = 99.99  # max lightness from original husl.py
L_MIN =  0.01


@optimized
@transform.rgb_float_input
def _rgb_to_husl(rgb_nd: ndarray) -> ndarray:
    """Convert a float (0 <= i <= 1.0) RGB image to an `ndarray`
    of HUSL values"""
    return _lch_to_husl(_rgb_to_lch(rgb_nd))


def _rgb_to_lch(rgb: ndarray) -> ndarray:
    return _luv_to_lch(_xyz_to_luv(_rgb_to_xyz(rgb)))


@optimized
@transform.rgb_float_input
def _rgb_to_hue(rgb: ndarray) -> ndarray:
    """Convenience function to return JUST the HUSL hue values
    for a given RGB image"""
    hsl = _rgb_to_husl(rgb)
    return _channel(hsl, 0)


def _rgb_to_xyz(rgb_nd: ndarray) -> ndarray:
    rgbl = _to_linear(rgb_nd)
    return _dot_product(constants.M_INV, rgbl)


@optimized
def _lch_to_husl(lch_nd: ndarray) -> ndarray:
    flat_shape = (lch_nd.size // 3, 3)
    lch_flat = lch_nd.reshape(flat_shape)
    _L, C, _H = (_channel(lch_flat, n) for n in range(3))
    hsl_flat = np.zeros(flat_shape, dtype=np.float)
    H, S, L = (_channel(hsl_flat, n) for n in range(3))
    H[:] = _H
    L[:] = _L

    # handle lightness extremes
    light = _L > L_MAX
    dark = _L < L_MIN
    S[light] = 0.0
    L[light] = 100.0
    S[dark] = 0.0
    L[dark] = 0.0

    # compute saturation for pixels that aren't too light or dark
    remaining = ~np.logical_or(light, dark)
    mx = _max_lh_chroma(lch_flat[remaining])
    S[remaining] = (C[remaining] / mx) * 100.0

    return hsl_flat.reshape(lch_nd.shape)


_2pi = math.pi * 2


@optimized
def _max_lh_chroma(lch: ndarray) -> ndarray:
    L, H = (_channel(lch, n) for n in (0, 2))
    hrad = (H / 360.0) * _2pi
    lengths = np.ndarray((lch.shape[0],), dtype=np.float)
    lengths[:] = np.inf
    for line in _bounds(L):
        lens = _ray_length(hrad, line)
        lens[np.isnan(lens)] = np.inf
        lens[lens < 0] = np.inf
        np.minimum(lens, lengths, out=lengths)
    return lengths


M_CONSTS = np.asarray(constants.M)
M1, M2, M3 = (M_CONSTS[..., n] for n in range(3))
TOP1_SCALAR = 284517.0 * M1 - 94839.0 * M3
TOP2_SCALAR = 838422.0 * M3 + 769860.0 * M2 + 731718.0 * M1
TOP2_L_SCALAR = 769860.0
BOTTOM_SCALAR = (632260.0 * M3 - 126452.0 * M2)
BOTTOM_CONST = 126452.0


@optimized
def _bounds(l_nd: ndarray) -> iter:
    sub1 = l_nd + 16.0
    np.power(sub1, 3, out=sub1)
    np.divide(sub1, 1560896.0, out=sub1)
    sub2 = sub1.flatten()  # flat copy
    lt_epsilon = sub2 < constants.EPSILON
    sub2[lt_epsilon] = (l_nd.flat[lt_epsilon] / constants.KAPPA)
    del lt_epsilon  # free NxM X sizeof(bool) memory?
    sub2 = sub2.reshape(sub1.shape)

    # The goal here is to compute "lines" for each lightness value
    # Since we can be dealing with LOTS of lightness values (i.e. 4,000 x
    # 6,000), this is implemented as an iterator. Raspberry Pi and other small
    # machines can't keep too many huge arrays in memory.
    for t1, t2, b in zip(TOP1_SCALAR, TOP2_SCALAR, BOTTOM_SCALAR):
        for t in (0, 1):
            top1 = sub2 * t1
            top2 = l_nd * sub2 * t2
            if t:
                top2 -= (l_nd * TOP2_L_SCALAR)
            bottom = sub2 * b
            if t:
                bottom += BOTTOM_CONST
            b1, b2 = top1 / bottom, top2 / bottom
            yield b1, b2


@optimized
def _ray_length(theta: ndarray, line: list) -> ndarray:
    m1, b1 = line
    length = b1 / (np.sin(theta) - m1 * np.cos(theta))
    return length


@optimized
def _luv_to_lch(luv_nd: ndarray) -> ndarray:
    uv_nd = _channel(luv_nd, slice(1, 2))
    uv_nd[uv_nd == -0.0] = 0.0   # -0.0 screws up atan2
    lch_nd = luv_nd.copy()
    L = luv_nd[..., 0]
    U, V = (_channel(luv_nd, n) for n in range(1, 3))
    C, H = (_channel(lch_nd, n) for n in range(1, 3))
    C = (U**2 + V**2)**0.5
    hrad = np.arctan2(V, U)
    H = np.degrees(hrad)
    if C.ndim == 0:
        if H < 0.0:
            H += 360
        lch_nd[:] = L, C, H
    else:
        H[H < 0.0] += 360.0
        lch_nd[..., 0] = L
        lch_nd[..., 1] = C
        lch_nd[..., 2] = H
    return lch_nd


@optimized
def _xyz_to_luv(xyz_nd: ndarray) -> ndarray:
    flat_shape = (xyz_nd.size // 3, 3)
    luv_flat = np.zeros(flat_shape, dtype=np.float)  # flattened luv n-dim array
    xyz_flat = xyz_nd.reshape(flat_shape)
    X, Y, Z = (_channel(xyz_flat, n) for n in range(3))

    with np.errstate(invalid="ignore"):  # ignore divide by zero
        U_var = (4 * X) / (X + (15 * Y) + (3 * Z))
        V_var = (9 * Y) / (X + (15 * Y) + (3 * Z))
    U_var[np.isinf(U_var)] = 0  # correct divide by zero
    V_var[np.isinf(V_var)] = 0  # correct divide by zero

    L, U, V = (_channel(luv_flat, n) for n in range(3))
    L[:] = _to_light(Y)
    luv_flat[L == 0] = 0
    U[:] = L * 13 * (U_var - constants.REF_U)
    V[:] = L * 13 * (V_var - constants.REF_V)
    luv_flat = np.nan_to_num(luv_flat)
    return luv_flat.reshape(xyz_nd.shape)


@optimized
def _to_light(y_nd: ndarray) -> ndarray:
    y_flat = y_nd.flatten()
    f_flat = np.zeros(y_flat.shape, dtype=np.float)
    gt = y_flat > constants.EPSILON
    f_flat[gt] = (y_flat[gt] / constants.REF_Y) ** (1.0 / 3.0) * 116 - 16
    f_flat[~gt] = (y_flat[~gt] / constants.REF_Y) * constants.KAPPA
    return f_flat.reshape(y_nd.shape)


@optimized
@transform.rgb_float_input
def _to_linear(rgb_nd: ndarray) -> ndarray:
    a = 0.055  # mysterious constant used in husl.to_linear
    xyz_nd = np.zeros(rgb_nd.shape, dtype=np.float)
    gt = rgb_nd > 0.04045
    xyz_nd[gt] = ((rgb_nd[gt] + a) / (1 + a)) ** 2.4
    xyz_nd[~gt] = rgb_nd[~gt] / 12.92
    return xyz_nd


@optimized
def _dot_product(scalars: list, rgb_nd: ndarray) -> ndarray:
    scalars = np.asarray(scalars, dtype=np.float)
    sum_axis = len(rgb_nd.shape) - 1
    x = np.sum(scalars[0] * rgb_nd, sum_axis)
    y = np.sum(scalars[1] * rgb_nd, sum_axis)
    z = np.sum(scalars[2] * rgb_nd, sum_axis)
    return np.dstack((x, y, z)).squeeze()


def _channel(data: ndarray, last_dim_idx) -> ndarray:
    return data[..., last_dim_idx]


### Conversions in the direction of HUSL -> RGB

@optimized
def _husl_to_rgb(husl_nd: ndarray) -> ndarray:
    return _lch_to_rgb(_husl_to_lch(husl_nd))


def _lch_to_rgb(lch_nd: ndarray) -> ndarray:
    return _xyz_to_rgb(_luv_to_xyz(_lch_to_luv(lch_nd)))


def _xyz_to_rgb(xyz_nd: ndarray) -> ndarray:
    xyz_dot = _dot_product(constants.M, xyz_nd)
    return _from_linear(xyz_dot)


def _lch_to_luv(lch_nd: ndarray) -> ndarray:
    luv_nd = np.zeros(lch_nd.shape, dtype=np.float)
    _L, C, H = (_channel(lch_nd, n) for n in range(3))
    L, U, V  = (_channel(luv_nd, n) for n in range(3))
    hrad = np.radians(H)
    U[:] = np.cos(hrad) * C
    V[:] = np.sin(hrad) * C
    L[:] = _L
    return luv_nd


def _from_linear(xyz_nd: ndarray) -> ndarray:
    rgb_nd = np.zeros(xyz_nd.shape, dtype=np.float)
    lt = xyz_nd <= 0.0031308
    rgb_nd[lt] = 12.92 * xyz_nd[lt]
    rgb_nd[~lt] = 1.055 * (xyz_nd[~lt] ** (1 / 2.4)) - 0.055
    return rgb_nd


def _husl_to_lch(husl_nd: ndarray) -> ndarray:
    flat_shape = (husl_nd.size // 3, 3)
    lch_flat = np.zeros(flat_shape, dtype=np.float)
    husl_flat = husl_nd.reshape(flat_shape)
    _H, S, _L = (_channel(husl_flat, n) for n in range(3))
    L, C, H = (_channel(lch_flat, n) for n in range(3))
    L[:] = _L
    H[:] = _H

    # compute max chroma for lightness and hue
    mx = _max_lh_chroma(lch_flat)
    C[:] = mx / 100.0 * S

    # handle lightness extremes
    light= L > L_MAX
    dark = L < L_MIN
    L[light] = 100
    C[light] = 0
    L[dark] = 0
    C[dark] = 0
    return lch_flat.reshape(husl_nd.shape)


def _luv_to_xyz(luv_nd: ndarray) -> ndarray:
    flat_shape = (luv_nd.size // 3, 3)
    xyz_flat = np.zeros(flat_shape, dtype=np.float)  # flattened xyz array
    luv_flat = luv_nd.reshape(flat_shape)
    L, U, V = (_channel(luv_flat, n) for n in range(3))
    X, Y, Z = (_channel(xyz_flat, n) for n in range(3))

    Y_var = _from_light(L)
    L13 = 13.0 * L
    with np.errstate(divide="ignore", invalid="ignore"):  # ignore divide by zero
        U_var = U / L13 + constants.REF_U
        V_var = V / L13 + constants.REF_V
    U_var[np.isinf(U_var)] = 0  # correct divide by zero
    V_var[np.isinf(V_var)] = 0  # correct divide by zero

    Y[:] = Y_var * constants.REF_Y
    with np.errstate(invalid="ignore"):
        X[:] = -(9 * Y * U_var) / ((U_var - 4.0) * V_var - U_var * V_var)
        Z[:] = (9.0 * Y - (15.0 * V_var * Y) - (V_var * X)) / (3.0 * V_var)
    xyz_flat[L == 0] = 0
    xyz_flat = np.nan_to_num(xyz_flat)
    return xyz_flat.reshape(luv_nd.shape)


def _from_light(l_nd: ndarray) -> ndarray:
    l_flat = l_nd.flatten()
    large = l_nd > 8
    small = ~large
    out = np.zeros(l_flat.shape, dtype=np.float)
    out[large] = constants.REF_Y * (((l_nd[large] + 16) / 116) ** 3.0)
    out[small] = constants.REF_Y * l_nd[small] / constants.KAPPA
    return out.reshape(l_nd.shape)

