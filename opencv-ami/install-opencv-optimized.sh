#!/bin/bash
# Install OpenCV optimized for AWS Graviton processors

set -e

echo "Installing OpenCV optimized for AWS Graviton..."

# Update system
sudo apt-get update -y

# Install development tools
sudo apt-get install -y build-essential
sudo apt-get install -y cmake python3-dev python3-pip

# Install dependencies
sudo apt-get install -y \
    libjpeg-turbo8-dev \
    libpng-dev \
    libtiff-dev \
    libwebp-dev \
    libopenjp2-7-dev \
    libeigen3-dev \
    libtbb-dev \
    libgtk-3-dev \
    libv4l-dev \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev

# Install Python packages
pip3 install --break-system-packages --user numpy

# Download OpenCV source
cd /tmp
wget -O opencv.zip https://github.com/opencv/opencv/archive/4.8.1.zip
wget -O opencv_contrib.zip https://github.com/opencv/opencv_contrib/archive/4.8.1.zip
unzip opencv.zip
unzip opencv_contrib.zip

# Create build directory
cd opencv-4.8.1
mkdir build
cd build

# Configure CMake for Graviton optimization
cmake \
    -D CMAKE_BUILD_TYPE=RELEASE \
    -D CMAKE_INSTALL_PREFIX=/usr/local \
    -D OPENCV_EXTRA_MODULES_PATH=/tmp/opencv_contrib-4.8.1/modules \
    -D PYTHON3_EXECUTABLE=/usr/bin/python3 \
    -D BUILD_opencv_python3=ON \
    -D BUILD_opencv_python2=OFF \
    -D CMAKE_C_FLAGS="-O3 -mcpu=native -mtune=native" \
    -D CMAKE_CXX_FLAGS="-O3 -mcpu=native -mtune=native" \
    -D WITH_TBB=ON \
    -D WITH_EIGEN=ON \
    -D WITH_V4L=ON \
    -D WITH_OPENGL=ON \
    -D WITH_OPENCL=ON \
    -D BUILD_TIFF=ON \
    -D BUILD_opencv_java=OFF \
    -D BUILD_SHARED_LIBS=ON \
    -D ENABLE_NEON=ON \
    -D CPU_BASELINE=NEON \
    -D CPU_DISPATCH=NEON,NEON_FP16 \
    ..

# Build OpenCV (use all available cores)
make -j$(nproc)

# Install OpenCV
sudo make install
sudo ldconfig

# Verify installation
python3 -c "import cv2; print('OpenCV version:', cv2.__version__)"

echo "OpenCV installation completed successfully!"

# Install additional Python dependencies for MCP server
pip3 install --break-system-packages --user \
    mcp \
    aiohttp \
    boto3 \
    psutil \
    Pillow

echo "All dependencies installed successfully!"