// HUSL color space conversion with OpenMP+SIMD
//
// Important functions:
// 1) rgb_to_husl_nd: RGB -> HUSL
// 2) husl_to_rgb_nd: HUSL -> RGB
// 3) rgb_to_hue_nd: RGB -> HUSL hue
// 4) rgb_to_lightness_nd: RGB -> HUSL lightness

#include <math.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
 

#ifdef _OPENMP
// Min array size for OpenMP parallelized loops
#define MIN_IMG_SIZE_THREADED 30*30*3
#include <omp.h>
#endif


#include <_simd.h>
#include <_linear_lookup.h>
#include <_scale_const.h>


#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif



////////////////////////////////////////////
// Conversion in the RGB -> HUSL direction
////////////////////////////////////////////


static double *allocate_hsl(size_t size);
static void rgb_to_luv_nd(uint8_t *rgb, double *luv, size_t size);
static void rgbluv_to_husl_nd(uint8_t *rgb, double *luv_hsl, size_t size);
static void to_linear_rgb(uint8_t r, uint8_t g, uint8_t b,
                          double *rl, double *gl, double *bl);
static void to_xyz(double r, double g, double b,
                   double *x, double *y, double *z);
static void to_luv(double x, double y, double z,
                   double *l, double *u, double *v);
static double to_light(double);
static double to_hue(double u, double v);
static double to_saturation(double, double, double, double);
static double max_chroma(double, double);


// Enable luminance lookup interpolation based on compile flag
#if defined(USE_LIGHT_LUT)
#include <_light_lookup.h>
#endif


// Choose LUV -> LCH croma function based on compile flag
#if defined(USE_CHROMA_LUT)
#include <_chroma_lookup.h>
#else
static double min_chroma_length(
    int iteration, double lightness, double sub1, double sub2,
    double top2, double top2_b, double sintheta, double costheta);
#endif


// Choose LUV -> Hue function based on compile flag
#if defined(USE_HUE_ATAN_APPROX)
static double atan_approx(double, double);
static double atan2_approx(double, double);
#elif defined(USE_HUE_ATAN2_APPROX)
static double atan2_approx(double, double);
#endif


// Constants for white pixels
static const double WHITE_HUE = 19.916405993809086;
static const double WHITE_SATURATION = 0.0;
static const double WHITE_LIGHTNESS = 100.0;


// RGB -> HUSL conversion
// Converts an array of c-contiguous RGB ints to an array of c-contiguous
// HSL doubles. RGB ints should be in the interval [0, 255]
double* rgb_to_husl_nd(uint8_t *rgb, size_t size) {
    // HUSL array of H, S, L triplets to be returned
    double *hsl = allocate_hsl(size);
    #pragma omp parallel \
        default(none) shared(rgb, hsl, size) \
        if (size >= MIN_IMG_SIZE_THREADED)
    { // begin OMP parallel
    rgb_to_luv_nd(rgb, hsl, size);
    #pragma omp barrier  // ensure LUV elements are done being written
    rgbluv_to_husl_nd(rgb, hsl, size);
    } // end OMP parallel
    return hsl;
}


// Aligned malloc for HUSL double arrays
static double* __attribute__((alloc_size(1))) allocate_hsl(size_t size) {
    double *hsl __attribute__((aligned(64))) = \
        (double*) malloc(size * sizeof(double));
    if (hsl == NULL) {
        fprintf(stderr, "Error: Couldn't allocate memory for HUSL array\n");
        exit(EXIT_FAILURE);
    }
    return hsl;
}


// Converts nonlinear RGB to CIE-LUV
static void rgb_to_luv_nd(uint8_t *rgb, double *luv, size_t size) {
    #pragma omp for schedule(static)
    for (unsigned int i = 0; i < size; i+=3) { 
        double *luv_p = luv + i;
        uint8_t *rgb_p = rgb + i;
        const uint8_t r = *(rgb_p);
        const uint8_t g = *(++rgb_p);
        const uint8_t b = *(++rgb_p);

        // from RGB in [0, 255] to RGB-linear in [0,1]
        double rl, gl, bl;
        to_linear_rgb(r, g, b, &rl, &gl, &bl);

        // to CIE-XYZ to CIE-LUV
        double x, y, z;
        double *l, *u, *v;
        l = luv_p;
        u = (++luv_p);
        v = (++luv_p);
        to_xyz(rl, gl, bl, &x, &y, &z);
        to_luv(x, y, z, l, u, v);
    }
}


// Convert RGB to linear RGB.
static inline void to_linear_rgb(uint8_t r, uint8_t g, uint8_t b,
                                 double *rl, double *gl, double *bl) {
    *rl = linear_table[r];
    *gl = linear_table[g];
    *bl = linear_table[b];
}


// Convert linear RGB to CIE XYZ space. See Celebi et al.
// Note that this is somewhat different than the husl.py reference
// implementation by Boronine, the creator of HUSL.
// See Celebi's paper "Fast Color Space
// Transformations Using Minimax Approximations".
static inline void to_xyz(double r, double g, double b,
                          double *x, double *y, double *z) {
    *x = 0.412391*r + 0.357584*g + 0.180481*b;
    *y = 0.212639*r + 0.715169*g + 0.072192*b;
    *z = 0.019331*r + 0.119195*g + 0.950532*b;
}


// Convert CIEXYZ to CIELUV
static inline void to_luv(double x, double y, double z,
                          double *l, double *u, double *v) {
    const double var_scale = x + 15*y + 3*z;
    const double var_u = 4*x / var_scale;
    const double var_v = 9*y / var_scale;
    *l = to_light(y);
    const double l13 = (*l)*13;
    *u = l13*(var_u - REF_U);
    *v = l13*(var_v - REF_V);
}


// Convert CIE-LUV to HUSL. The original RGB array is still passed in
// for the handling of boundary conditions (white and black pixels).
static void rgbluv_to_husl_nd(uint8_t *rgb, double *luv_hsl, size_t size) {
    #pragma omp for schedule(guided)
    for (unsigned int i = 0; i < size; i+=3) {
        double *hsl_p = luv_hsl + i;
        uint8_t *rgb_p = rgb + i;

        const uint8_t r = *rgb_p;
        const uint8_t g = *(++rgb_p);
        const uint8_t b = *(++rgb_p);

        if (r == 255 && g == 255 && b == 255) {
            *(hsl_p) = WHITE_HUE;
            *(++hsl_p) = WHITE_SATURATION;
            *(++hsl_p) = WHITE_LIGHTNESS;
        } else if (!r && !g && !b) {
            *(hsl_p) = 0;
            *(++hsl_p) = 0;
            *(++hsl_p) = 0;
        } else {
            // This is the most expensive part of the RGB->HUSL chain
            const double l = *hsl_p;
            const double u = *(hsl_p+1);
            const double v = *(hsl_p+2);
            const double h = to_hue(u, v);
            const double s = to_saturation(l, u, v, h);
            *(hsl_p) = h;
            *(++hsl_p) = s;
            *(++hsl_p) = l;
        }
    } // end OMP for
}

static const double DEG_PER_RAD = 180.0 / M_PI;


#if defined(USE_HUE_ATAN2_APPROX)

static double to_hue(double u, double v) {
    double hue = atan2_approx(v, u) * DEG_PER_RAD;
    if (hue < 0) {
        hue += 360;
    }
    return hue;
}


// The standard to_hue function uses an expensive call to atan2
// http://dspguru.com/dsp/tricks/fixed-point-atan2-with-self-normalization
static double atan2_approx(double y, double x) {
    static const double PI_4 = M_PI / 4.0;
    static const double PI_3_4 = 3.0 * M_PI / 4.0;
    double r, angle;
    double abs_y = fabs(y) + 1e-10f;  // prevents divide-by-zero
    if (x < 0.0f) {
        r = (x + abs_y) / (abs_y - x);
        angle = PI_3_4;
    } else {
        r = (x - abs_y) / (x + abs_y);
        angle = PI_4;
    }
    angle += (0.1963f * r * r - 0.9817f) * r;
    if (y < 0.0f) {
        return -angle;  // negate if in quad III or IV
    } else {
        return angle;
    }
}

#else

// Standard to_hue function
// Uses a costly atan2 call

static inline double to_hue(double u, double v) {
    double hue = atan2f(v, u) * DEG_PER_RAD;
    if (hue < 0) {
        hue += 360;
    }
    return hue;
}
#endif


// Returns a saturation value from UV (of CIELUV), lightness, and hue.
// Saturation magnitude (hypotenuse b/t U & V) is found via sqrt(U**2 + V**2),
// then it's normalized by the max chroma, which is dictated by H and L.
static inline double to_saturation(double l, double u, double v, double h) {
    return 100*sqrt(u*u + v*v) / max_chroma(l, h);
}


#if defined(USE_CHROMA_LUT)
// Returns a maximum chroma value  given an L, H pair.
// Uses the chroma lookup table to perform bilinear interpolation.
// This LUT approach is important, because finding the max chroma is
// the most expensive operation in RGB -> HUSL conversion.
// Reference (see Unit Square section):
// https://en.wikipedia.org/wiki/Bilinear_interpolation
static inline double max_chroma(double lightness, double hue) {
    // Compute H-value indices (axis 0) and L-value indices (axis 1)
    double h_idx = hue / h_idx_step;
    double l_idx = lightness / l_idx_step;
    unsigned short h_idx_floor = floor(h_idx); 
    unsigned short l_idx_floor = floor(l_idx);
    h_idx_floor = fmax(0, fmin(C_TABLE_SIZE-2, h_idx_floor));
    l_idx_floor = fmax(0, fmin(C_TABLE_SIZE-2, l_idx_floor));

    // Find four known f() values in the unit square bilinear interp. approach
    double chroma_00 = chroma_table[h_idx_floor][l_idx_floor];
    double chroma_10 = chroma_table[h_idx_floor+1][l_idx_floor];
    double chroma_01 = chroma_table[h_idx_floor][l_idx_floor+1];
    double chroma_11 = chroma_table[h_idx_floor+1][l_idx_floor+1];

    // Find *normalized* x, y, (1-x), and (1-y) values
    // It's a coordinate system where the four known chromas are at
    // (0,0), (1,0), (0,1), and (1,1), so we normalize hue and luminance.
    double h_norm = h_idx - h_idx_floor;  // our "x" value
    double l_norm = l_idx - l_idx_floor;  // our "y" value
    double h_inv = 1 - h_norm;  // (1-x)
    double l_inv = 1 - l_norm;  // (1-y)
    
    // Compute f(x,y) = f(0,0)(1-x)(1-y) + f(1,0)x(1-y) + f(0,1)(1-x)y + f(1,1)xy
    double interp_sq = chroma_00*h_inv*l_inv +
                       chroma_10*h_norm*l_inv +
                       chroma_01*h_inv*l_norm +
                       chroma_11*h_norm*l_norm;
    return interp_sq;
} 


#else
// Returns max chroma given an L, H pair.
// Very expensive operation.
static double max_chroma(double lightness, double hue) {
    double sub1 = pow(lightness + 16.0, 3) / 1560896.0;
    double sub2 = sub1 > EPSILON ? sub1 : lightness / KAPPA;
    double top2 = SCALE_SUB2 * lightness * sub2;
    double top2_b = top2 - 769860.0*lightness;
    double theta = hue / 360.0 * M_PI * 2.0;  // hue in radians
    double sintheta = sinf(theta);
    double costheta = cosf(theta);
    double len0, len1, len2;
    len0 = min_chroma_length(
        0, lightness, sub1, sub2,
        top2, top2_b, sintheta, costheta);
    len1 = min_chroma_length(
        1, lightness, sub1, sub2,
        top2, top2_b, sintheta, costheta);
    len2 = min_chroma_length(
        2, lightness, sub1, sub2,
        top2, top2_b, sintheta, costheta);
    return fmin(fmin(len0, len1), len2);
}


// Returns a min chroma "length" in the HSL space from H and L.
// The HUSL color space is basically CIELUV, but
// it overcomes its doubleing chroma value by "stretching"
// the color space so that a new channel, "saturation"
// is a percentage in [0, 100] for all possible values of hue and lightness.
// The images at husl-colors.org explain this more clearly!
static inline double min_chroma_length(
        int iteration, double lightness, double sub1, double sub2,
        double top2, double top2_b, double sintheta, double costheta) {
    double top1 = SCALE_SUB1[iteration] * sub2;
    double bottom = SCALE_BOTTOM[iteration] * sub2;
    double bottom_b = bottom + 126452.0;
    double min_length = 10000.0;
    double len;

    len = (top2 / bottom) / (sintheta - (top1 / bottom) * costheta);
    min_length = len > 0 ? len : min_length;
    len = (top2_b / bottom_b) / (sintheta - (top1 / bottom_b) * costheta);
    min_length = len > 0 ? fmin(len, min_length) : min_length;
    return min_length;
}
#endif


// Define a function that takes the Y of the XYZ space
// and returns a lightness value. If -DUSE_LIGHT_LUT is passed to GCC,
// a faster-but-less-accurate lookup table approach will be used.

#if defined(USE_LIGHT_LUT)

// Return a light value from a CIEXYZ Y-value.
// A light value lookup that accounts for a nonlinear
// relationship between Y, the input, and L, the output.
// The lookup table is combined from three smaller
// tables, each with a different Y-value to L-value scale.
static double to_light(double y_value) {
    double idx;
    if (y_value < y_thresh_0) {
        idx = y_value/y_idx_step_0;
    } else if (y_value < y_thresh_1) {
        idx = ((y_value - y_thresh_0)/y_idx_step_1) + L_SEGMENT_SIZE;
    } else {
        idx = ((y_value - y_thresh_1)/y_idx_step_2) + L_SEGMENT_SIZE*2;
    }
    const uint_fast16_t idx_floor = fmax(0, fmin(L_FULL_TABLE_SIZE-1, roundf(idx)));
    return light_table_big[idx_floor];
}

#else

// Return a light value from a CIEXYZ Y-value.
// Uses an expensive branch/cube-root.
static inline double to_light(double y_value) {
    if (y_value > EPSILON) {
        return 116 * cbrt(y_value / REF_Y) - 16;
    } else {
        return (y_value / REF_Y) * KAPPA;
    }
}

#endif // end to_light conditional definition


//
// Conversion in the HUSL -> RGB direction
//




