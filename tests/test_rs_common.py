from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gst_realsense_launch.rs_common import ConfigLoader, parse_resolution


@pytest.fixture
def mock_config_loader(tmp_path):
    """
    Create a fake config.yaml and ports.yaml for testing.
    """
    config_content = """
network:
  server_ip: "127.0.0.1"
  base_port: 5000
  stream_ports:
    color:
      rtp: 5000
    depth:
      rtp: 5002
    imu:
      udp: 5050
camera:
  resolution: "640x480"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    ports_content = """
udp:
  gstreamer:
    realsense_streams:
      color:
        rtp: 8000
      depth:
        rtp: 8002
"""
    ports_file = tmp_path / "ports.yaml"
    ports_file.write_text(ports_content)

    loader = ConfigLoader(config_file=str(config_file), ports_file=str(ports_file))
    return loader


def test_config_loader_loads_values(mock_config_loader):
    """Test whether ConfigLoader can read the setting values ​​correctly."""
    assert mock_config_loader.get("network.server_ip") == "127.0.0.1"
    assert mock_config_loader.get("camera.resolution") == "640x480"
    assert mock_config_loader.get("network.base_port", default=9999) == 5000


def test_config_loader_override(mock_config_loader):
    """Test that the override functionality is working properly."""
    assert mock_config_loader.get("network.server_ip", override="192.168.1.100") == "192.168.1.100"


def test_get_stream_ports_from_ports_yaml(mock_config_loader):
    """Test whether the streaming port can be read from ports.yaml."""
    ports = mock_config_loader.get_stream_ports()
    assert ports.get("color") == 8000
    assert ports.get("depth") == 8002


def test_get_port_for_stream(mock_config_loader):
    """Test the ability to get a specific streaming port."""
    assert mock_config_loader.get_port_for_stream("color", fallback_port=9999) == 5000
    assert mock_config_loader.get_port_for_stream("depth", fallback_port=9999) == 5002
    assert mock_config_loader.get_port_for_stream("imu", fallback_port=9999) == 5050


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
