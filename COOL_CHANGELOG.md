# Changelog
All notable changes to this project will be documented in this file.

# COOL (Cloud Optimized OpenCV Library) 

All benchmarks measured on 
-  **AWS Graviton4 `m8g.xlarge`** (4 vCPUs, 16 GB RAM) 
-  **Ubuntu 24.04** 
-  **GCC 13.3** 
-  **Baseline: OpenCV 4.13 (pip)** , same instance.

---

## v2.0.0 — 2026-04-21
This release introduces massive performance improvements targeting the ARM Neoverse-V2 architecture. 

### Highlights

- **3.22× average speedup** across top 20 optimized functions vs. stock OpenCV 4.13
- **59.8% average latency reduction**, with peak gains of **10.5×** on Laplacian edge detection
- All optimizations pass OpenCV accuracy and conformance test suites

### Optimized Functions 

| # | Function | What it does | Speedup |
|--:|----------|--------------|--------:|
| 1 | `Laplacian`  | Detects edges by computing second-order derivatives  | **10.50×** |
| 2 | `adaptiveThreshold` (mean) | Local binarization over 11×11 mean | **7.70×** |
| 3 | `Scharr`  | High-accuracy 3×3 gradient | **4.25×** |
| 4 | `calcHist` (gray, 256 bins) | Builds a 256-bin intensity histogram for a single-channel grayscale image | **4.12×** |
| 5 | `cvtColor` (BGR→Gray) | Converts a 3-channel BGR color image to single-channel grayscale using the luminance formula | **4.00×** |
| 6 | `Sobel` (dx) | Horizontal gradient via separable 3×3 kernel | **3.67×** |
| 7 | `Sobel` (dy) | Vertical gradient via separable 3×3 kernel | **3.33×** |
| 8 | `calcHist` (BGR, 32 bins) | Computes a joint 3D color histogram with 32 bins per channel for BGR images | **2.81×** |
| 9 | `matchTemplate` (NCC) | Slides a template across the image computing normalized cross-correlation at each position | **2.57×** |
| 10 | `threshold` (Otsu) | Binarizes an image using Otsu's method to automatically determine the optimal threshold | **2.43×** |
| 11 | `resize` (2×) | Downsamples an image by half using area-based interpolation | **2.42×** |
| 12 | `adaptiveThreshold` (gauss) | Gaussian-weighted local threshold | **2.24×** |
| 13 | `bilateralFilter`  | Smooths an image while preserving edges | **2.24×** |
| 14 | `cvtColor` (BGR→RGB) | Channel swap in a 3-channel image | **2.00×** |
| 15 | `distanceTransform` | Computes the Euclidean distance from every pixel to the nearest zero-valued pixel | **1.82×** |
| 16 | `GaussianBlur` (15×15) | Mid-range 15×15 Gaussian smoothing | **1.79×** |
| 17 | `imwrite` (BMP) | Encodes and writes an image in uncompressed format | **1.73×** |
| 18 | `GaussianBlur` (21×21) | Heavy 21×21 Gaussian smoothing  | **1.63×** |
| 19 | `resize` (cubic) | Upscales an image using bicubic interpolation for smooth visual results | **1.59×** |
| 20 | `goodFeaturesToTrack`  | Detects corner points using the Shi-Tomasi or Harris criteria | **1.58×** |

---

## v1.0.0 — 2026-02-20

Initial release. 
