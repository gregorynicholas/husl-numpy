import sys
from setuptools import setup, Extension
from nphusl import __version__
import numpy

CYTHONIZE = "--cythonize" in sys.argv
NO_CYTHON_EXT = "--no-cython-ext" in sys.argv
NO_SIMD_EXT = "--no-simd-ext" in sys.argv

if CYTHONIZE:
    sys.argv.remove("--cythonize")
if NO_SIMD_EXT:
    sys.argv.remove("--no-simd-ext")
if NO_CYTHON_EXT:
    sys.argv.remove("--no-cython-ext")


url = "https://github.com/TadLeonard/husl-numpy"
download = "{}/archive/{}.tar.gz".format(url, __version__)

long_description = """
HUSL color space conversion
===========================

A color space conversion library that works with numpy. See
http://husl-colors.org to learn about the HUSL color space.


Features
--------

1. Fast conversion to RGB from HUSL and vice versa. Convert a 1080p image to
HUSL in less than a second.
2. Seamless performance improvements with `NumExpr`, `Cython`, and `OpenMP`
(whichever's available).
3. Flexible `numpy` arrays as inputs and outputs. Plays nicely with `OpenCV`,
`MoviePy`, etc.

Installation
------------

1. `virtualenv env -p python3`
2. `source env/bin/activate`
3. `pip install numpy`
4. (optional) `pip install Cython`  (or NumExpr, but Cython is preferred)
5. `pip install git+https://github.com/TadLeonard/husl-numpy.git`

Basic usage
-----------

* `to_rgb(hsl)` Convert HUSL array to RGB integer array
* `to_husl(rgb)` Convert RGB integer array or grayscale float array to HUSL
* array
* `to_hue(rgb)` Convert RGB integer array or grayscale float array to array of
* hue values

More
====

See {url} for complete documentation.
""".format(url=url)

description = ("A HUSL color space conversion library that works with numpy")

classifiers = [
  'Development Status :: 5 - Production/Stable',
  'Intended Audience :: Developers',
  'Operating System :: OS Independent',
  'Programming Language :: Python :: 3',
  'Programming Language :: Python :: 3 :: Only',
  'Intended Audience :: Science/Research',
  'Topic :: Multimedia',
  'Topic :: Multimedia :: Graphics',
]

ext = '.pyx' if CYTHONIZE else '.c'
extensions = []
cython_compile_args = ["-fopenmp", "-O3", "-ffast-math"]
simd_compile_args = [
     #-DUSE_LINEAR_RGB_LUT",
     "-DUSE_SEGMENTED_LIGHT_LUT",
     #"-DUSE_LINEAR_LIGHT_LUT",
     #"-DUSE_CHROMA_LUT,"
     "-ftree-vectorize",
     "-ftree-vectorizer-verbose=8",
     #"-msse3",
     #"-mveclibabi=acml"
] + cython_compile_args

cython_ext = Extension("nphusl._cython_opt",
                       sources=["nphusl/_cython_opt"+ext],
                       extra_compile_args=cython_compile_args,
                       extra_link_args=["-fopenmp"])
simd_ext = Extension("nphusl._simd_opt",
                     sources=["nphusl/_simd_opt"+ext,
                              "nphusl/_simd.c",
                              "nphusl/_linear_lookup.c",
                              "nphusl/_scale_const.c",
                              "nphusl/_light_lookup.c",
                              "nphusl/_chroma_lookup.c",],
                     extra_compile_args=simd_compile_args,
                     include_dirs=["nphusl/"],
                     extra_link_args=["-fopenmp",])

if not NO_CYTHON_EXT:
    extensions.append(cython_ext)
if not NO_SIMD_EXT:
    extensions.append(simd_ext)
if CYTHONIZE:
    from Cython.Build import cythonize
    extensions = cythonize(extensions)


setup(name='nphusl',
      version=__version__,
      packages=["nphusl"],
      install_requires=["numpy"],
      ext_modules=extensions,
      include_dirs=[numpy.get_include()],
      licence="MIT",
      description=description,
      long_description=long_description,
      classifiers=classifiers,
      author="Tad Leonard",
      author_email="tadfleonard@gmail.com",
      keywords="husl hsl color conversion rgb image processing",
      url=url,
      download_url=download,
      zip_safe=False,
)


