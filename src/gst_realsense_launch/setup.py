import os
from glob import glob

from setuptools import find_packages, setup

package_name = "gst_realsense_launch"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
        (os.path.join("share", package_name, "config"), glob("config/*.yaml")),
    ],
    install_requires=[
        "setuptools",
        "requests>=2.31.0",
        "pyyaml>=6.0",
        "pyrealsense2>=2.56.5.9235",
        "numpy<2",
    ],
    zip_safe=True,
    maintainer="cthsu",
    maintainer_email="chuntsehsu@gmail.com",
    description="GStreamer RealSense launch and camera info",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "gst_sender = startup.gst_sender:main",
            "gst_receiver = startup.gst_receiver:main",
            "rs_camera_info_parser = startup.rs_camera_info_parser:main",
            "network_diagnostics = startup.network_diagnostics:main",
            "depth_merger_node = node.depth_merger_node:main",
            "imu_receiver_node = node.imu_receiver_node:main",
            "tf_odom_publisher_node = node.tf_odom_publisher_node:main",
        ],
    },
)
