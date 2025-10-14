from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

from launch import LaunchDescription  # type: ignore[attr-defined]
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution


def generate_launch_description():
    # Args
    declare_depth_width = DeclareLaunchArgument("depth_width", default_value="424")
    declare_depth_height = DeclareLaunchArgument("depth_height", default_value="240")
    declare_color_width = DeclareLaunchArgument("color_width", default_value="424")
    declare_color_height = DeclareLaunchArgument("color_height", default_value="240")
    declare_depth_port = DeclareLaunchArgument("depth_port", default_value="5000")
    declare_color_port = DeclareLaunchArgument("color_port", default_value="5002")

    depth_width = LaunchConfiguration("depth_width")
    depth_height = LaunchConfiguration("depth_height")
    color_width = LaunchConfiguration("color_width")
    color_height = LaunchConfiguration("color_height")
    depth_port = LaunchConfiguration("depth_port")
    color_port = LaunchConfiguration("color_port")

    pkg_share = FindPackageShare("gst_realsense_launch")

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

    # Depth (JPEG2000 → GRAY16_LE → mono16)
    depth_node = Node(
        package="gscam",
        executable="gscam_node",
        name="depth_camera",
        parameters=[
            {
                "gscam_config": [
                    "udpsrc port=",
                    depth_port,
                    " ",
                    'caps="application/x-rtp,media=video,encoding-name=JPEG2000,',
                    'payload=96,clock-rate=90000" ',
                    # avdec_jpeg2000 or openjpegdec
                    "! rtpj2kdepay ! jpeg2000parse ! avdec_jpeg2000 ",
                    "! videoconvert ! video/x-raw,format=GRAY16_LE",
                ],
                "image_encoding": "mono16",  #  16-bit
                "use_gst_timestamps": True,
                "use_sensor_data_qos": True,
                "sync_sink": False,
                "reopen_on_eof": True,
                "camera_name": "realsense_depth_424x240",
                "camera_info_url": depth_camera_info_url,
                "frame_id": "camera_depth_optical_frame",
            }
        ],
        remappings=[
            ("camera/image_raw", "/camera_0/depth/image"),
            ("camera/camera_info", "/camera_0/depth/camera_info"),
        ],
    )

    # Color (H264 → RGB → rgb8)
    color_node = Node(
        package="gscam",
        executable="gscam_node",
        name="color_camera",
        parameters=[
            {
                "gscam_config": [
                    "udpsrc port=",
                    color_port,
                    " ",
                    'caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"',
                    "! rtpjitterbuffer latency=100 drop-on-late=true"
                    "! rtph264depay ! h24parse ! avdec_h264 ",
                    "! videoconvert ! video/x-raw,format=RGB",
                ],
                "image_encoding": "rgb8",
                "use_gst_timestamps": True,
                "use_sensor_data_qos": True,
                "sync_sink": False,
                "reopen_on_eof": True,
                "camera_name": "realsense_color_424x240",
                "camera_info_url": color_camera_info_url,
                "frame_id": "camera_color_optical_frame",
            }
        ],
        remappings=[
            ("camera/image_raw", "/camera_0/color/image"),
            ("camera/camera_info", "/camera_0/color/camera_info"),
        ],
    )

    depth_to_float_node = Node(
        package="depth_image_proc",
        executable="convert_metric_node",
        name="depth_to_float",
        remappings=[
            ("image_raw", "/camera_0/depth/image"),
            ("camera_info", "/camera_0/depth/camera_info"),
            ("image", "/camera_0/depth/image_float"),  # 32FC1
        ],
    )

    return LaunchDescription(
        [
            declare_depth_width,
            declare_depth_height,
            declare_color_width,
            declare_color_height,
            declare_depth_port,
            declare_color_port,
            depth_node,
            color_node,
            depth_to_float_node,
        ]
    )
