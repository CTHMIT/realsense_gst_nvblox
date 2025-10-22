#!/usr/bin/env python3
"""
RealSense Virtual Camera Launch File for isaac_ros_nvblox

This launch file receives RealSense streams over network and publishes them
to ROS2 topics compatible with isaac_ros_nvblox.

包含所有 ROS2 功能:
- Video stream reception (via gscam)
- IMU data reception and publishing
- TF tree publishing (camera frames + odom)
- Odometry publishing
"""

from launch import LaunchDescription  # type: ignore[attr-defined]
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    """Generate launch description for RealSense virtual camera."""

    # ========================================================================
    # LAUNCH ARGUMENTS
    # ========================================================================
    camera_name_arg = DeclareLaunchArgument(
        "camera_name", default_value="camera", description="Camera name for topics and frames"
    )

    depth_port_arg = DeclareLaunchArgument(
        "depth_port", default_value="5020", description="Port for depth stream"
    )

    color_port_arg = DeclareLaunchArgument(
        "color_port", default_value="5010", description="Port for color stream"
    )

    infra1_port_arg = DeclareLaunchArgument(
        "infra1_port", default_value="5030", description="Port for infra1 stream (Y8I)"
    )

    infra2_port_arg = DeclareLaunchArgument(
        "infra2_port",
        default_value="5030",
        description="Port for infra2 stream (Y8I, same as infra1)",
    )

    imu_port_arg = DeclareLaunchArgument(
        "imu_port", default_value="5050", description="Port for IMU data"
    )

    depth_width_arg = DeclareLaunchArgument(
        "depth_width", default_value="640", description="Depth image width"
    )

    depth_height_arg = DeclareLaunchArgument(
        "depth_height", default_value="480", description="Depth image height"
    )

    color_width_arg = DeclareLaunchArgument(
        "color_width", default_value="640", description="Color image width"
    )

    color_height_arg = DeclareLaunchArgument(
        "color_height", default_value="480", description="Color image height"
    )

    infra_width_arg = DeclareLaunchArgument(
        "infra_width", default_value="640", description="Infrared image width (single camera)"
    )

    infra_height_arg = DeclareLaunchArgument(
        "infra_height", default_value="480", description="Infrared image height"
    )

    use_depth_float_arg = DeclareLaunchArgument(
        "use_depth_float",
        default_value="true",
        description="Convert depth to 32FC1 format (required for nvblox)",
    )

    enable_infra_arg = DeclareLaunchArgument(
        "enable_infra", default_value="false", description="Enable infrared streams"
    )

    enable_imu_arg = DeclareLaunchArgument(
        "enable_imu", default_value="true", description="Enable IMU receiver"
    )

    publish_odom_arg = DeclareLaunchArgument(
        "publish_odom", default_value="true", description="Publish odometry messages"
    )

    odom_frame_arg = DeclareLaunchArgument(
        "odom_frame", default_value="odom", description="Odometry frame ID"
    )

    base_link_frame_arg = DeclareLaunchArgument(
        "base_link_frame", default_value="base_link", description="Base link frame ID"
    )

    local_ip_arg = DeclareLaunchArgument(
        "local_ip",
        default_value="0.0.0.0",
        description="Local IP address to bind IMU receiver to",
    )

    # LaunchConfiguration
    camera_name = LaunchConfiguration("camera_name")
    depth_port = LaunchConfiguration("depth_port")
    color_port = LaunchConfiguration("color_port")
    infra1_port = LaunchConfiguration("infra1_port")
    infra2_port = LaunchConfiguration("infra2_port")
    imu_port = LaunchConfiguration("imu_port")
    depth_width = LaunchConfiguration("depth_width")
    depth_height = LaunchConfiguration("depth_height")
    color_width = LaunchConfiguration("color_width")
    color_height = LaunchConfiguration("color_height")
    infra_width = LaunchConfiguration("infra_width")
    infra_height = LaunchConfiguration("infra_height")
    use_depth_float = LaunchConfiguration("use_depth_float")
    enable_infra = LaunchConfiguration("enable_infra")
    enable_imu = LaunchConfiguration("enable_imu")
    publish_odom = LaunchConfiguration("publish_odom")
    odom_frame = LaunchConfiguration("odom_frame")
    base_link_frame = LaunchConfiguration("base_link_frame")
    local_ip = LaunchConfiguration("local_ip")

    pkg_share = FindPackageShare("gst_realsense_launch")

    # Camera info URLs
    depth_camera_info_url = [
        "file://",
        PathJoinSubstitution(
            [pkg_share, "config", ["depth_camera_", depth_width, "x", depth_height, ".yaml"]]
        ),
    ]

    color_camera_info_url = [
        "file://",
        PathJoinSubstitution(
            [pkg_share, "config", ["color_camera_", color_width, "x", color_height, ".yaml"]]
        ),
    ]

    infrared_camera_info_url = [
        "file://",
        PathJoinSubstitution(
            [pkg_share, "config", ["infrared_camera_", infra_width, "x", infra_height, ".yaml"]]
        ),
    ]

    # ========================================================================
    # DEPTH CAMERA NODE
    # ========================================================================
    depth_gscam_node = Node(
        package="gscam",
        executable="gscam_node",
        name="depth_gscam",
        namespace=camera_name,
        parameters=[
            {
                "gscam_config": [
                    "udpsrc port=",
                    depth_port,
                    " buffer-size=2097152 ",
                    'caps="application/x-rtp,media=video,clock-rate=90000,'
                    'encoding-name=H264,payload=96" ',
                    "! rtpjitterbuffer latency=200 drop-on-latency=true ",
                    "! rtph264depay ! h264parse ! avdec_h264 max-threads=4 ",
                    "! queue max-size-buffers=4 leaky=downstream ",
                    "! videoconvert n-threads=4 ! video/x-raw,format=GRAY16_LE",
                ],
                "camera_name": [camera_name, "_depth"],
                "camera_info_url": depth_camera_info_url,
                "frame_id": [camera_name, "_depth_optical_frame"],
                "image_encoding": "16UC1",
                "sync_sink": False,
                "use_gst_timestamps": True,
                "reopen_on_eof": True,
            }
        ],
        remappings=[
            ("camera/image_raw", "depth/image_rect_raw"),
            ("camera/camera_info", "depth/camera_info"),
        ],
        output="screen",
    )

    # ========================================================================
    # DEPTH TO FLOAT CONVERTER
    # ========================================================================
    depth_to_float_node = Node(
        package="depth_image_proc",
        executable="convert_metric_node",
        name="depth_to_float",
        namespace=camera_name,
        condition=IfCondition(use_depth_float),
        remappings=[
            ("image_raw", "depth/image_rect_raw"),
            ("camera_info", "depth/camera_info"),
            ("image", "depth/image"),
        ],
        output="screen",
    )

    # ========================================================================
    # COLOR CAMERA NODE
    # ========================================================================
    color_gscam_node = Node(
        package="gscam",
        executable="gscam_node",
        name="color_gscam",
        namespace=camera_name,
        parameters=[
            {
                "gscam_config": [
                    "udpsrc port=",
                    color_port,
                    " buffer-size=2097152 ",
                    'caps="application/x-rtp,media=video,clock-rate=90000,'
                    'encoding-name=H264,payload=98" ',
                    "! rtpjitterbuffer latency=200 drop-on-latency=true ",
                    "! rtph264depay ! h264parse ! avdec_h264 max-threads=4 ",
                    "! videoconvert n-threads=4 ! video/x-raw,format=RGB",
                ],
                "camera_name": [camera_name, "_color"],
                "camera_info_url": color_camera_info_url,
                "frame_id": [camera_name, "_color_optical_frame"],
                "image_encoding": "rgb8",
                "sync_sink": False,
                "use_gst_timestamps": True,
                "reopen_on_eof": True,
            }
        ],
        remappings=[
            ("camera/image_raw", "color/image_raw"),
            ("camera/camera_info", "color/camera_info"),
        ],
        output="screen",
    )

    # ========================================================================
    # INFRARED 1 CAMERA NODE (LEFT)
    # ========================================================================
    infra1_gscam_node = Node(
        package="gscam",
        executable="gscam_node",
        name="infra1_gscam",
        namespace=camera_name,
        condition=IfCondition(enable_infra),
        parameters=[
            {
                "gscam_config": [
                    "udpsrc port=",
                    infra1_port,
                    " buffer-size=2097152 ",
                    'caps="application/x-rtp,media=video,clock-rate=90000,'
                    'encoding-name=H264,payload=99" ',
                    "! rtpjitterbuffer latency=200 ",
                    "! rtph264depay ! h264parse ! avdec_h264 max-threads=4 ",
                    "! videoconvert ! video/x-raw,format=GRAY8 ",
                    "! videocrop right=",
                    infra_width,
                ],
                "camera_name": [camera_name, "_infra1"],
                "camera_info_url": infrared_camera_info_url,
                "frame_id": [camera_name, "_infra1_optical_frame"],
                "image_encoding": "mono8",
                "sync_sink": False,
                "use_gst_timestamps": True,
                "reopen_on_eof": True,
            }
        ],
        remappings=[
            ("camera/image_raw", "infra1/image_rect_raw"),
            ("camera/camera_info", "infra1/camera_info"),
        ],
        output="screen",
    )

    # ========================================================================
    # INFRARED 2 CAMERA NODE (RIGHT)
    # ========================================================================
    infra2_gscam_node = Node(
        package="gscam",
        executable="gscam_node",
        name="infra2_gscam",
        namespace=camera_name,
        condition=IfCondition(enable_infra),
        parameters=[
            {
                "gscam_config": [
                    "udpsrc port=",
                    infra2_port,
                    " buffer-size=2097152 ",
                    'caps="application/x-rtp,media=video,clock-rate=90000,'
                    'encoding-name=H264,payload=99" ',
                    "! rtpjitterbuffer latency=200 ",
                    "! rtph264depay ! h264parse ! avdec_h264 max-threads=4 ",
                    "! videoconvert ! video/x-raw,format=GRAY8 ",
                    "! videocrop left=",
                    infra_width,
                ],
                "camera_name": [camera_name, "_infra2"],
                "camera_info_url": infrared_camera_info_url,
                "frame_id": [camera_name, "_infra2_optical_frame"],
                "image_encoding": "mono8",
                "sync_sink": False,
                "use_gst_timestamps": True,
                "reopen_on_eof": True,
            }
        ],
        remappings=[
            ("camera/image_raw", "infra2/image_rect_raw"),
            ("camera/camera_info", "infra2/camera_info"),
        ],
        output="screen",
    )

    # ========================================================================
    # IMU RECEIVER NODE
    # ========================================================================
    imu_receiver_node = Node(
        package="gst_realsense_launch",
        executable="imu_receiver_node.py",
        name="imu_receiver",
        namespace=camera_name,
        condition=IfCondition(enable_imu),
        parameters=[
            {
                "imu_port": imu_port,
                "local_ip": local_ip,
                "frame_id": [camera_name, "_imu_optical_frame"],
            }
        ],
        remappings=[
            ("imu", "imu"),
        ],
        output="screen",
    )

    # ========================================================================
    # TF AND ODOMETRY PUBLISHER NODE
    # ========================================================================
    tf_odom_publisher_node = Node(
        package="gst_realsense_launch",
        executable="tf_odom_publisher_node.py",
        name="tf_odom_publisher",
        namespace=camera_name,
        parameters=[
            {
                "camera_name": camera_name,
                "publish_odom": publish_odom,
                "odom_frame": odom_frame,
                "base_link_frame": base_link_frame,
            }
        ],
        remappings=[
            ("odom", "odom"),
        ],
        output="screen",
    )

    # ========================================================================
    # STATIC TRANSFORM PUBLISHERS
    # ========================================================================
    # camera_link -> depth_frame
    depth_frame_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="depth_frame_tf",
        arguments=[
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "1",
            [camera_name, "_link"],
            [camera_name, "_depth_frame"],
        ],
    )

    # depth_frame -> depth_optical_frame
    depth_optical_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="depth_optical_tf",
        arguments=[
            "0",
            "0",
            "0",
            "-0.5",
            "0.5",
            "-0.5",
            "0.5",
            [camera_name, "_depth_frame"],
            [camera_name, "_depth_optical_frame"],
        ],
    )

    # camera_link -> color_frame
    color_frame_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="color_frame_tf",
        arguments=[
            "0.015",
            "0",
            "0",
            "0",
            "0",
            "0",
            "1",
            [camera_name, "_link"],
            [camera_name, "_color_frame"],
        ],
    )

    # color_frame -> color_optical_frame
    color_optical_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="color_optical_tf",
        arguments=[
            "0",
            "0",
            "0",
            "-0.5",
            "0.5",
            "-0.5",
            "0.5",
            [camera_name, "_color_frame"],
            [camera_name, "_color_optical_frame"],
        ],
    )

    # camera_link -> infra1_frame
    infra1_frame_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="infra1_frame_tf",
        condition=IfCondition(enable_infra),
        arguments=[
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "1",
            [camera_name, "_link"],
            [camera_name, "_infra1_frame"],
        ],
    )

    # infra1_frame -> infra1_optical_frame
    infra1_optical_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="infra1_optical_tf",
        condition=IfCondition(enable_infra),
        arguments=[
            "0",
            "0",
            "0",
            "-0.5",
            "0.5",
            "-0.5",
            "0.5",
            [camera_name, "_infra1_frame"],
            [camera_name, "_infra1_optical_frame"],
        ],
    )

    # camera_link -> infra2_frame
    infra2_frame_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="infra2_frame_tf",
        condition=IfCondition(enable_infra),
        arguments=[
            "0.050",
            "0",
            "0",
            "0",
            "0",
            "0",
            "1",
            [camera_name, "_link"],
            [camera_name, "_infra2_frame"],
        ],
    )

    # infra2_frame -> infra2_optical_frame
    infra2_optical_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="infra2_optical_tf",
        condition=IfCondition(enable_infra),
        arguments=[
            "0",
            "0",
            "0",
            "-0.5",
            "0.5",
            "-0.5",
            "0.5",
            [camera_name, "_infra2_frame"],
            [camera_name, "_infra2_optical_frame"],
        ],
    )

    # camera_link -> imu_optical_frame
    imu_optical_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="imu_optical_tf",
        condition=IfCondition(enable_imu),
        arguments=[
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "1",
            [camera_name, "_link"],
            [camera_name, "_imu_optical_frame"],
        ],
    )

    # ========================================================================
    # BUILD LAUNCH DESCRIPTION
    # ========================================================================
    return LaunchDescription(
        [
            # Arguments
            camera_name_arg,
            depth_port_arg,
            color_port_arg,
            infra1_port_arg,
            infra2_port_arg,
            imu_port_arg,
            depth_width_arg,
            depth_height_arg,
            color_width_arg,
            color_height_arg,
            infra_width_arg,
            infra_height_arg,
            use_depth_float_arg,
            enable_infra_arg,
            enable_imu_arg,
            publish_odom_arg,
            odom_frame_arg,
            base_link_frame_arg,
            local_ip_arg,
            # Camera nodes
            depth_gscam_node,
            depth_to_float_node,
            color_gscam_node,
            infra1_gscam_node,
            infra2_gscam_node,
            # ROS2 功能節點
            imu_receiver_node,
            tf_odom_publisher_node,
            # Static transforms
            depth_frame_tf,
            depth_optical_tf,
            color_frame_tf,
            color_optical_tf,
            infra1_frame_tf,
            infra1_optical_tf,
            infra2_frame_tf,
            infra2_optical_tf,
            imu_optical_tf,
        ]
    )
