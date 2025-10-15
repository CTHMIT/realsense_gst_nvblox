from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gst_realsense_launch.rs_common import ConfigLoader, parse_resolution


@pytest.fixture
def mock_config_loader(tmp_path):
    """
    Create a fake config.yaml for testing with unified configuration.
    """
    config_content = """
network:
  server_ip: "127.0.0.1"
  base_port: 5000
  stream_ports:
    color:
      rtp: 5000
      rtcp: 5001
    depth:
      rtp: 5002
      rtcp: 5003
    infra1:
      rtp: 5004
      rtcp: 5005
    infra2:
      rtp: 5006
      rtcp: 5007
    imu:
      udp: 5050
camera:
  name: "camera0"
  resolution: "640x480"
  fps: 30
encoding:
  encoder: "auto"
  bitrate: 4000
  jpeg2000_quality: 100
imu:
  enabled: true
  publish_rate: 200.0
receiver:
  show_views: false
  view_scale: 0.5
  publish_odom: true
  odom_frame: "odom"
  base_link_frame: "base_link"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    # Create ConfigLoader with only config_file (unified configuration)
    loader = ConfigLoader(config_file=str(config_file))
    return loader


def test_config_loader_loads_values(mock_config_loader):
    """Test whether ConfigLoader can read the setting values correctly."""
    assert mock_config_loader.get("network.server_ip") == "127.0.0.1"
    assert mock_config_loader.get("camera.resolution") == "640x480"
    assert mock_config_loader.get("network.base_port", default=9999) == 5000
    assert mock_config_loader.get("camera.name") == "camera0"


def test_config_loader_override(mock_config_loader):
    """Test that the override functionality is working properly."""
    assert mock_config_loader.get("network.server_ip", override="192.168.1.100") == "192.168.1.100"


def test_get_stream_ports_from_config(mock_config_loader):
    """Test whether the streaming port can be read from unified config.yaml."""
    # Test that we can get ports directly from network.stream_ports
    stream_ports = mock_config_loader.get("network.stream_ports")
    assert stream_ports is not None
    assert stream_ports["color"]["rtp"] == 5000
    assert stream_ports["depth"]["rtp"] == 5002
    assert stream_ports["imu"]["udp"] == 5050


def test_get_port_for_stream(mock_config_loader):
    """Test the ability to get a specific streaming port."""
    # Test RTP ports for video streams
    assert mock_config_loader.get_port_for_stream("color", fallback_port=9999) == 5000
    assert mock_config_loader.get_port_for_stream("depth", fallback_port=9999) == 5002
    assert mock_config_loader.get_port_for_stream("infra1", fallback_port=9999) == 5004
    assert mock_config_loader.get_port_for_stream("infra2", fallback_port=9999) == 5006

    # Test UDP port for IMU
    assert mock_config_loader.get_port_for_stream("imu", fallback_port=9999) == 5050

    # Test fallback for non-existent stream
    assert mock_config_loader.get_port_for_stream("nonexistent", fallback_port=9999) == 9999


def test_get_imu_port(mock_config_loader):
    """Test getting IMU port from unified configuration."""
    imu_port = mock_config_loader.get_imu_port()
    assert imu_port == 5050


def test_get_network_config(mock_config_loader):
    """Test getting complete network configuration."""
    # Create a mock args object
    mock_args = MagicMock()
    mock_args.host = None
    mock_args.base_port = None
    mock_args.imu_port = None
    mock_args.metadata_port = None
    mock_args.info_url = None

    network_cfg = mock_config_loader.get_network_config(mock_args)

    assert network_cfg["server_ip"] == "127.0.0.1"
    assert network_cfg["base_port"] == 5000
    assert network_cfg["imu_port"] == 5050


def test_get_network_config_with_override(mock_config_loader):
    """Test network config with command line overrides."""
    mock_args = MagicMock()
    mock_args.host = "192.168.1.100"
    mock_args.base_port = 6000
    mock_args.imu_port = 6050
    mock_args.metadata_port = None
    mock_args.info_url = None

    network_cfg = mock_config_loader.get_network_config(mock_args)

    assert network_cfg["server_ip"] == "192.168.1.100"
    assert network_cfg["base_port"] == 6000
    assert network_cfg["imu_port"] == 6050


def test_get_camera_config(mock_config_loader):
    """Test getting complete camera configuration."""
    mock_args = MagicMock()
    mock_args.resolution = None
    mock_args.fps = None
    mock_args.camera_name = None
    mock_args.stream = None

    camera_cfg = mock_config_loader.get_camera_config(mock_args)

    assert camera_cfg["resolution"] == "640x480"
    assert camera_cfg["fps"] == 30
    assert camera_cfg["camera_name"] == "camera0"


def test_get_encoding_config(mock_config_loader):
    """Test getting encoding configuration."""
    mock_args = MagicMock()
    mock_args.encoder = None
    mock_args.bitrate = None

    encoding_cfg = mock_config_loader.get_encoding_config(mock_args)

    assert encoding_cfg["encoder"] == "auto"
    assert encoding_cfg["bitrate"] == 4000
    assert encoding_cfg["jpeg2000_quality"] == 100


def test_get_imu_config(mock_config_loader):
    """Test getting IMU configuration."""
    mock_args = MagicMock()
    mock_args.no_imu = False

    imu_cfg = mock_config_loader.get_imu_config(mock_args)

    assert imu_cfg["enabled"] is True
    assert imu_cfg["publish_rate"] == 200.0


def test_get_receiver_config(mock_config_loader):
    """Test getting receiver configuration."""
    mock_args = MagicMock()
    mock_args.show_views = False
    mock_args.view_scale = None
    mock_args.publish_odom = None

    receiver_cfg = mock_config_loader.get_receiver_config(mock_args)

    assert receiver_cfg["show_views"] is False
    assert receiver_cfg["view_scale"] == 0.5
    assert receiver_cfg["publish_odom"] is True
    assert receiver_cfg["odom_frame"] == "odom"
    assert receiver_cfg["base_link_frame"] == "base_link"


def test_parse_resolution_valid():
    """Tests whether the resolution parsing function can handle valid input."""
    width, height = parse_resolution("1280x720")
    assert width == 1280
    assert height == 720


def test_parse_resolution_invalid():
    """Tests whether the resolution parsing function can handle invalid input and raise an error."""
    with pytest.raises(ValueError, match="Invalid resolution format"):
        parse_resolution("1280,720")
    with pytest.raises(ValueError, match="Dimensions must be positive"):
        parse_resolution("-640x480")


def test_create_default_intrinsics(mock_config_loader):
    """Test creating default camera intrinsics."""
    intrinsics = mock_config_loader.create_default_intrinsics(640, 480)

    assert intrinsics.width == 640
    assert intrinsics.height == 480
    assert intrinsics.fx == 640 * 0.6  # fx_ratio from config
    assert intrinsics.fy == 480 * 0.6  # fy_ratio from config
    assert intrinsics.ppx == 640 * 0.5  # ppx_ratio from config
    assert intrinsics.ppy == 480 * 0.5  # ppy_ratio from config


def test_config_loader_missing_file():
    """Test ConfigLoader behavior with non-existent config file."""
    loader = ConfigLoader(config_file="/nonexistent/path/config.yaml")

    # Should use defaults when file doesn't exist
    assert loader.get("network.server_ip") is None
    assert loader.get("network.base_port", default=5000) == 5000
