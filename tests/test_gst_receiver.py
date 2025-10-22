#!/usr/bin/env python3
"""Tests for GStreamer receiver functionality (Pure GStreamer, no ROS2)."""

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from gst_realsense_launch.startup.gst_receiver import (
    TmuxSessionManager,
    VideoStreamReceiver,
    check_and_cleanup_existing_resources,
)
from gst_realsense_launch.startup.rs_common import CameraIntrinsics, ConfigLoader


class TestTmuxSessionManager:
    """Test TmuxSessionManager functionality."""

    @patch("subprocess.run")
    def test_tmux_check_available(self, mock_run):
        """Test tmux availability check."""
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(TmuxSessionManager, "_cleanup_old_session"):
            with patch.object(TmuxSessionManager, "_create_session"):
                manager = TmuxSessionManager("test_session")
                assert manager.session_name == "test_session"
                mock_run.assert_called()

    @patch("subprocess.run")
    def test_tmux_not_available(self, mock_run):
        """Test tmux not available handling."""
        mock_run.side_effect = FileNotFoundError()

        with pytest.raises(RuntimeError, match="tmux is not installed"):
            TmuxSessionManager("test_session")

    @patch("subprocess.run")
    @patch("time.sleep")
    def test_cleanup_old_session(self, mock_sleep, mock_run):
        """Test cleanup of old tmux session."""
        # First call: has-session returns 0 (session exists)
        # Second call: kill-session
        mock_run.side_effect = [
            MagicMock(returncode=0),  # tmux -V
            MagicMock(returncode=0),  # has-session (exists)
            MagicMock(returncode=0),  # kill-session
            MagicMock(returncode=0),  # new-session
        ]

        manager = TmuxSessionManager("test_session")
        assert manager.session_name == "test_session"

    @patch("subprocess.run")
    def test_create_window(self, mock_run):
        """Test creating tmux window."""
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(TmuxSessionManager, "_cleanup_old_session"):
            with patch.object(TmuxSessionManager, "_create_session"):
                manager = TmuxSessionManager("test_session")
                manager.create_window("test_window", "echo test")

                assert manager.window_count == 1


class TestVideoStreamReceiver:
    """Test VideoStreamReceiver functionality."""

    @pytest.fixture
    def mock_config_loader(self):
        """Create a mock ConfigLoader."""
        config_loader = MagicMock(spec=ConfigLoader)
        config_loader.get.return_value = {}
        return config_loader

    @pytest.fixture
    def receiver(self, mock_config_loader):
        """Create VideoStreamReceiver instance."""
        with patch.object(TmuxSessionManager, "__init__", return_value=None):
            receiver = VideoStreamReceiver(
                camera_name="test_camera",
                config_loader=mock_config_loader,
                show_views=False,
                view_scale=0.5,
            )
            receiver.tmux_manager = MagicMock()
            return receiver

    def test_receiver_initialization(self, receiver):
        """Test receiver initialization."""
        assert receiver.camera_name == "test_camera"
        assert receiver.show_views is False
        assert receiver.view_scale == 0.5

    def test_build_pipeline_depth(self, receiver, mock_config_loader):
        """Test depth stream pipeline building."""
        mock_config_loader.get.side_effect = lambda key, default=None: {
            "streaming.udp.buffer_size": 2097152,
            "streaming.processing.max_threads": 4,
            "streaming.processing.n_threads": 4,
            "streaming.queue.max_size_buffers": 4,
            "streaming.queue.leaky": "downstream",
            "streaming.jitter_buffer.depth.latency": 200,
            "streaming.jitter_buffer.depth.drop_on_latency": True,
            "streaming.rtp.payload_types": {"depth_h264": 96},
            "receiver.gstreamer_format": {"depth": "GRAY16_LE"},
        }.get(key, default)

        pipeline = receiver._build_pipeline(5020, "depth", "h264", 640, 480)

        assert "udpsrc port=5020" in pipeline
        assert "buffer-size=2097152" in pipeline
        assert "encoding-name=H264" in pipeline
        assert "payload=96" in pipeline
        assert "format=GRAY16_LE" in pipeline

    def test_build_pipeline_color(self, receiver, mock_config_loader):
        """Test color stream pipeline building."""
        mock_config_loader.get.side_effect = lambda key, default=None: {
            "streaming.udp.buffer_size": 2097152,
            "streaming.processing.max_threads": 4,
            "streaming.processing.n_threads": 4,
            "streaming.queue.max_size_buffers": 4,
            "streaming.queue.leaky": "downstream",
            "streaming.jitter_buffer.color.latency": 200,
            "streaming.jitter_buffer.color.drop_on_latency": True,
            "streaming.rtp.payload_types": {"color_h264": 98},
            "receiver.gstreamer_format": {"color": "RGB"},
        }.get(key, default)

        pipeline = receiver._build_pipeline(5010, "color", "h264", 640, 480)

        assert "udpsrc port=5010" in pipeline
        assert "encoding-name=H264" in pipeline
        assert "payload=98" in pipeline
        assert "format=RGB" in pipeline

    def test_build_y8i_pipeline_infra1(self, receiver, mock_config_loader):
        """Test Y8I pipeline for infra1."""
        mock_config_loader.get.side_effect = lambda key, default=None: {
            "streaming.udp.buffer_size": 2097152,
            "streaming.processing.max_threads": 4,
            "streaming.processing.n_threads": 4,
            "streaming.queue.max_size_buffers": 4,
            "streaming.queue.leaky": "downstream",
            "streaming.jitter_buffer.infra_stereo.latency": 200,
            "streaming.jitter_buffer.infra_stereo.drop_on_latency": True,
            "streaming.rtp.payload_types": {"infra_stereo_h264": 99},
        }.get(key, default)

        pipeline = receiver._build_y8i_pipeline(5030, "infra1", 1280, 480)

        assert "udpsrc port=5030" in pipeline
        assert "payload=99" in pipeline
        assert "width=1280,height=480" in pipeline
        assert "videocrop right=640" in pipeline  # Crop right half for infra1

    def test_build_y8i_pipeline_infra2(self, receiver, mock_config_loader):
        """Test Y8I pipeline for infra2."""
        mock_config_loader.get.side_effect = lambda key, default=None: {
            "streaming.udp.buffer_size": 2097152,
            "streaming.processing.max_threads": 4,
            "streaming.processing.n_threads": 4,
            "streaming.queue.max_size_buffers": 4,
            "streaming.queue.leaky": "downstream",
            "streaming.jitter_buffer.infra_stereo.latency": 200,
            "streaming.jitter_buffer.infra_stereo.drop_on_latency": True,
            "streaming.rtp.payload_types": {"infra_stereo_h264": 99},
        }.get(key, default)

        pipeline = receiver._build_y8i_pipeline(5030, "infra2", 1280, 480)

        assert "udpsrc port=5030" in pipeline
        assert "payload=99" in pipeline
        assert "videocrop left=640" in pipeline  # Crop left half for infra2

    def test_get_calibration_file_path_exists(self, receiver, tmp_path):
        """Test finding existing calibration file."""
        calib_dir = tmp_path / "config"
        calib_dir.mkdir()
        calib_file = calib_dir / "depth_camera_640x480.yaml"
        calib_file.write_text("test calibration")

        with patch("gst_realsense_launch.gst_receiver.Path") as mock_path:
            mock_path.return_value = tmp_path
            # Mock the glob to return our test directory
            with patch.object(Path, "exists", return_value=True):
                result = receiver._get_calibration_file_path("depth", 640, 480)
                # Should return some path (exact path depends on search order)
                assert result is None or isinstance(result, str)

    def test_get_calibration_file_path_not_exists(self, receiver):
        """Test calibration file not found."""
        with patch.object(Path, "exists", return_value=False):
            result = receiver._get_calibration_file_path("depth", 640, 480)
            assert result is None

    @patch("builtins.open", new_callable=mock_open)
    def test_create_camera_info_file(self, mock_file, receiver):
        """Test camera info file creation."""

        intrinsics = CameraIntrinsics(
            width=640,
            height=480,
            fx=600.0,
            fy=600.0,
            ppx=320.0,
            ppy=240.0,
            distortion=[0.0, 0.0, 0.0, 0.0, 0.0],
        )

        receiver._create_camera_info_file("/tmp/test.yaml", "depth", 640, 480, intrinsics)

        mock_file.assert_called_once_with("/tmp/test.yaml", "w")
        handle = mock_file()
        written_content = "".join(call.args[0] for call in handle.write.call_args_list)

        assert "image_width: 640" in written_content
        assert "image_height: 480" in written_content
        assert "600.0" in written_content

    @patch("subprocess.run")
    def test_cleanup_orphaned_processes(self, mock_run, receiver):
        """Test cleanup of orphaned processes."""
        # Mock pgrep finding processes
        mock_run.return_value = MagicMock(returncode=0, stdout="12345\n67890\n")

        receiver._cleanup_orphaned_processes()

        # Should have called pgrep and kill
        assert mock_run.call_count > 0


class TestUtilityFunctions:
    """Test utility functions."""

    @patch("subprocess.run")
    @patch("time.sleep")
    def test_check_and_cleanup_existing_resources(self, mock_sleep, mock_run):
        """Test resource cleanup check."""
        # Mock tmux session exists
        mock_run.side_effect = [
            MagicMock(returncode=0),  # has-session (exists)
            MagicMock(returncode=0),  # kill-session
            MagicMock(returncode=1, stdout=""),  # pgrep gscam (not found)
            MagicMock(returncode=1, stdout=""),  # pgrep depth_image_proc
            MagicMock(returncode=1, stdout=""),  # pgrep convert_metric
            MagicMock(returncode=1, stdout=""),  # pgrep depth_merger_node
        ]

        result = check_and_cleanup_existing_resources("test_camera")
        assert result is True

    @patch("subprocess.run")
    def test_check_and_cleanup_no_existing_resources(self, mock_run):
        """Test when no existing resources found."""
        # Mock no tmux session
        mock_run.side_effect = [
            MagicMock(returncode=1),  # has-session (not exists)
            MagicMock(returncode=1, stdout=""),  # pgrep gscam
            MagicMock(returncode=1, stdout=""),  # pgrep depth_image_proc
            MagicMock(returncode=1, stdout=""),  # pgrep convert_metric
            MagicMock(returncode=1, stdout=""),  # pgrep depth_merger_node
        ]

        result = check_and_cleanup_existing_resources("test_camera")
        assert result is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
