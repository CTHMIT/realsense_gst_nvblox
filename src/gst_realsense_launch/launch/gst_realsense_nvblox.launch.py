#!/usr/bin/env python3
"""Launch file for isaac_ros_nvblox with GStreamer RealSense streams

This launch file integrates your GStreamer-based RealSense depth and color streams
with NVIDIA's isaac_ros_nvblox for real-time 3D reconstruction.

Prerequisites:
    - isaac_ros_nvblox installed
    - isaac_ros_visual_slam installed (optional, for odometry)
    - Your GStreamer receiver publishing:
        /camera/color/image_raw
        /camera/color/camera_info
        /camera/depth/image_rect_raw
        /camera/depth/camera_info

Usage:
    # With Visual SLAM (recommended):
    ros2 launch gst_realsense_launch gst_realsense_nvblox.launch.py

    # Without Visual SLAM (provide odometry externally):
    ros2 launch gst_realsense_launch gst_realsense_nvblox.launch.py enable_vslam:=False

    # With RViz visualization:
    ros2 launch gst_realsense_launch gst_realsense_nvblox.launch.py enable_rviz:=True

    # Custom mesh resolution:
    ros2 launch gst_realsense_launch gst_realsense_nvblox.launch.py voxel_size:=0.05
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution, TextSubstitution
from launch_ros.actions import Node, SetRemap
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    """Generate launch description for nvblox with GStreamer RealSense."""

    # Launch arguments
    camera_name_arg = DeclareLaunchArgument(
        "camera_name",
        default_value="camera",
        description="Camera name (should match your GStreamer receiver)",
    )

    enable_vslam_arg = DeclareLaunchArgument(
        "enable_vslam",
        default_value="True",
        description="Enable isaac_ros_visual_slam for odometry estimation",
    )

    enable_rviz_arg = DeclareLaunchArgument(
        "enable_rviz", default_value="False", description="Enable RViz2 visualization"
    )

    voxel_size_arg = DeclareLaunchArgument(
        "voxel_size",
        default_value="0.05",
        description="Voxel size in meters (smaller = more detail, higher compute)",
    )

    max_integration_distance_arg = DeclareLaunchArgument(
        "max_integration_distance",
        default_value="10.0",
        description="Maximum integration distance in meters",
    )

    mapping_type_arg = DeclareLaunchArgument(
        "mapping_type",
        default_value="static_tsdf",
        choices=["static_tsdf", "static_occupancy", "dynamic"],
        description="Mapping type: static_tsdf (default), static_occupancy, or dynamic",
    )

    # Get launch configurations
    camera_name = LaunchConfiguration("camera_name")
    enable_vslam = LaunchConfiguration("enable_vslam")
    enable_rviz = LaunchConfiguration("enable_rviz")
    voxel_size = LaunchConfiguration("voxel_size")
    max_integration_distance = LaunchConfiguration("max_integration_distance")
    mapping_type = LaunchConfiguration("mapping_type")

    # Frame IDs (adjust if your GStreamer setup uses different names)
    base_frame = TextSubstitution(text="base_link")
    camera_frame = [camera_name, TextSubstitution(text="_link")]
    depth_optical_frame = [camera_name, TextSubstitution(text="_depth_optical_frame")]
    color_optical_frame = [camera_name, TextSubstitution(text="_color_optical_frame")]

    visual_slam_node = Node(
        package="isaac_ros_visual_slam",
        executable="isaac_ros_visual_slam",
        name="visual_slam",
        parameters=[
            {
                "num_cameras": 1,
                "min_num_images": 2,
                "enable_imu_fusion": True,
                "enable_rectified_pose": True,
                "rectified_images": True,
                "enable_observations_view": False,
                "enable_landmarks_view": False,
                "enable_reading_slam_internals": False,
                "enable_slam_visualization": False,
                "enable_localization_n_mapping": True,
                "path_max_size": 1024,
                "base_frame": base_frame,
                "map_frame": "map",
                "odom_frame": "odom",
                "denoise_input_images": False,
            }
        ],
        remappings=[
            ("visual_slam/image_0", [camera_name, TextSubstitution(text="/color/image_raw")]),
            (
                "visual_slam/camera_info_0",
                [camera_name, TextSubstitution(text="/color/camera_info")],
            ),
            ("visual_slam/imu", [camera_name, TextSubstitution(text="/imu/data")]),
        ],
        condition=IfCondition(enable_vslam),
        output="screen",
    )

    # ============================================================================
    # Nvblox Node (3D Reconstruction)
    # ============================================================================
    nvblox_node = Node(
        package="nvblox_ros",
        executable="nvblox_node",
        name="nvblox_node",
        parameters=[
            {
                # Voxel settings
                "voxel_size": voxel_size,
                "esdf_voxel_size": voxel_size,  # Same as TSDF voxel size
                # Integration settings
                "max_integration_distance_m": max_integration_distance,
                "max_tsdf_update_hz": 10.0,
                "max_color_update_hz": 5.0,
                "max_mesh_update_hz": 5.0,
                "max_esdf_update_hz": 2.0,
                # TSDF integrator settings
                "tsdf_integrator_max_integration_distance_m": max_integration_distance,
                "tsdf_integrator_truncation_distance_vox": 4.0,
                "tsdf_integrator_max_weight": 100.0,
                # Mesh settings
                "mesh_integrator_min_weight": 0.5,
                "mesh_integrator_weld_vertices": True,
                # ESDF settings (for path planning)
                "esdf_integrator_min_weight": 0.5,
                "esdf_integrator_max_site_distance_vox": 1.0,
                "esdf_integrator_max_distance_m": 10.0,
                # Mapping type
                "mapping_type": mapping_type,
                # Frame IDs
                "global_frame": "map",
                "pose_frame": "base_link",
                # Layer visualization
                "slice_visualization_attachment_frame_id": "base_link",
                "slice_visualization_side_length": 10.0,
                # Performance
                "use_depth": True,
                "use_color": True,
                "use_lidar": False,
                # Depth processing
                "depth_preprocessing_num_dilations": 4,
                # Memory management
                "layer_cake_size": 20.0,
                "layer_cake_height": 4.0,
            }
        ],
        remappings=[
            ("depth/image", [camera_name, TextSubstitution(text="/depth/image_rect_raw")]),
            ("depth/camera_info", [camera_name, TextSubstitution(text="/depth/camera_info")]),
            ("color/image", [camera_name, TextSubstitution(text="/color/image_raw")]),
            ("color/camera_info", [camera_name, TextSubstitution(text="/color/camera_info")]),
            ("transform", "visual_slam/tracking/odometry"),  # If using VSLAM
            ("pose", "visual_slam/tracking/vo_pose"),  # If using VSLAM
        ],
        output="screen",
    )

    # ============================================================================
    # Static Transform (camera to base_link)
    # ============================================================================
    # Adjust these values based on your robot's camera mounting position
    camera_transform_publisher = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="camera_tf_publisher",
        arguments=[
            "0",
            "0",
            "0.1",  # x, y, z (camera 10cm above base_link)
            "0",
            "0",
            "0",  # roll, pitch, yaw
            "base_link",
            camera_frame,
        ],
        output="screen",
    )

    # Optical frame transforms (standard RealSense optical frames)
    depth_optical_transform = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="depth_optical_tf",
        arguments=[
            "0",
            "0",
            "0",
            "-1.57079632679",
            "0",
            "-1.57079632679",  # Rotate to optical frame
            camera_frame,
            depth_optical_frame,
        ],
        output="screen",
    )

    color_optical_transform = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="color_optical_tf",
        arguments=[
            "0",
            "0",
            "0",
            "-1.57079632679",
            "0",
            "-1.57079632679",  # Rotate to optical frame
            camera_frame,
            color_optical_frame,
        ],
        output="screen",
    )

    # ============================================================================
    # RViz (Optional)
    # ============================================================================
    rviz_config_path = PathJoinSubstitution(
        [FindPackageShare("nvblox_examples"), "config", "nvblox_realsense.rviz"]
    )

    rviz_node = Node(
        package="rviz2",
        executable="rviz2",
        name="rviz2",
        arguments=["-d", rviz_config_path],
        condition=IfCondition(enable_rviz),
        output="screen",
    )

    # ============================================================================
    # Optional: Republish depth as 32FC1 (if nvblox requires it)
    # ============================================================================
    # Some versions of nvblox may expect 32FC1 depth instead of 16UC1
    # Uncomment if needed:
    # depth_conversion_node = Node(
    #     package='depth_image_proc',
    #     executable='convert_metric_node',
    #     name='depth_converter',
    #     remappings=[
    #         ('image_raw', [camera_name, TextSubstitution(text='/depth/image_rect_raw')]),
    #         ('camera_info', [camera_name, TextSubstitution(text='/depth/camera_info')]),
    #         ('image', [camera_name, TextSubstitution(text='/depth/image_32fc1')]),
    #     ],
    #     output='screen'
    # )

    # ============================================================================
    # Launch Description
    # ============================================================================
    return LaunchDescription(
        [
            # Arguments
            camera_name_arg,
            enable_vslam_arg,
            enable_rviz_arg,
            voxel_size_arg,
            max_integration_distance_arg,
            mapping_type_arg,
            # Nodes
            visual_slam_node,
            nvblox_node,
            camera_transform_publisher,
            depth_optical_transform,
            color_optical_transform,
            rviz_node,
        ]
    )
