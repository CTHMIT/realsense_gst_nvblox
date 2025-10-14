import math

from gst_realsense_launch.gst_receiver import VirtualRealSenseNode


def test_receiver_placeholder():
    """
    This is a placeholder test to ensure that the pytest test suite executes smoothly.
    This archive can be expanded in the future to include more receiver-specific tests.
    """
    assert True


def test_euler_to_quaternion():
    """Test the Euler angle to quaternion function."""
    roll, pitch, yaw = 0, 0, math.pi / 2
    qx, qy, qz, qw = VirtualRealSenseNode._euler_to_quaternion(roll, pitch, yaw)

    assert math.isclose(qx, 0)
    assert math.isclose(qy, 0)
    assert math.isclose(qz, math.sin(math.pi / 4))
    assert math.isclose(qw, math.cos(math.pi / 4))
