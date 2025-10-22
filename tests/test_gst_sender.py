import pytest

from gst_realsense_launch.startup.gst_sender import DeviceInfo, Mode, find_best_mode


def test_sender_placeholder():
    """A placeholder test to ensure pytest can find this file."""
    assert True


@pytest.fixture
def sample_device_info():
    """Create a fake DeviceInfo object for testing."""
    modes = [
        Mode(fourcc="Z16 ", size=(640, 480), fps_list=[30, 60]),
        Mode(fourcc="YUYV", size=(640, 480), fps_list=[30]),
        Mode(fourcc="YUYV", size=(1280, 720), fps_list=[15, 30]),
    ]
    return DeviceInfo(dev="/dev/video0", card="TestCam", model="D435i", serial="123", modes=modes)


def test_find_best_mode_exact_match(sample_device_info):
    """Test whether it can be found correctly when there is an exact matching resolution."""
    target_size = (640, 480)
    mode = find_best_mode(sample_device_info, target_size, "color")
    assert mode is not None
    assert mode.size == target_size
    assert mode.fourcc == "YUYV"


def test_find_best_mode_closest_match(sample_device_info):
    """Tests whether the closest resolution can be found when there is no exact matching resolution."""
    target_size = (800, 600)
    mode = find_best_mode(sample_device_info, target_size, "color")
    assert mode is not None
    assert mode.size == (640, 480)


def test_find_best_mode_no_match(sample_device_info):
    """Tests whether None is returned when there is no pattern matching the streaming type."""
    target_size = (640, 480)
    mode = find_best_mode(sample_device_info, target_size, "non_existent_type")
    assert mode is None
