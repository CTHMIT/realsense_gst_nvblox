#!/usr/bin/env python3
"""
Comprehensive Configuration System for RealSense D435i GStreamer Streaming

Provides type-safe, validated configuration with hardware-aware defaults
and optimized GStreamer pipeline parameters.
"""

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ==============================================================================
# Enumerations
# ==============================================================================

class PlatformType(str, Enum):
    """Hardware platform types."""
    JETSON = "jetson"
    X86_64 = "x86_64"
    AUTO = "auto"


class EncoderType(str, Enum):
    """Video encoder types."""
    NVENC = "nvh264enc"
    X264 = "x264enc"
    VAAPI = "vaapih264enc"


class TransportProtocol(str, Enum):
    """Network transport protocols."""
    UDP = "udp"
    TCP = "tcp"


class QoSReliability(str, Enum):
    """ROS2 QoS reliability."""
    RELIABLE = "reliable"
    BEST_EFFORT = "best_effort"


# ==============================================================================
# Platform Configuration
# ==============================================================================

class PlatformConfig(BaseModel):
    """Hardware platform configuration."""
    type: PlatformType = PlatformType.JETSON
    device: str = Field(default="agx_orin", description="Specific device model")
    cuda_available: bool = True
    nvenc_available: bool = True
    
    @field_validator('device')
    @classmethod
    def validate_device(cls, v: str) -> str:
        """Validate device name."""
        valid_devices = {"agx_orin", "orin_nano", "xavier", "nx", "desktop"}
        if v not in valid_devices:
            raise ValueError(f"Device must be one of {valid_devices}")
        return v


# ==============================================================================
# Network Configuration
# ==============================================================================

class TransportConfig(BaseModel):
    """Network transport configuration."""
    protocol: TransportProtocol = TransportProtocol.UDP
    mtu: int = Field(default=1500, ge=576, le=9000, description="Network MTU")


class QoSConfig(BaseModel):
    """Quality of Service configuration."""
    dscp: int = Field(default=46, ge=0, le=63)
    tos: int = Field(default=184, ge=0, le=255)


class NetworkConfig(BaseModel):
    """Network configuration."""
    local_ip: str = Field(default="0.0.0.0", description="Binding IP")
    server_ip: str = Field(description="Target receiver IP")
    transport: TransportConfig = TransportConfig()
    qos: QoSConfig = QoSConfig()
    
    @field_validator('local_ip', 'server_ip')
    @classmethod
    def validate_ip(cls, v: str) -> str:
        """Basic IP validation."""
        if v != "0.0.0.0" and not all(
            0 <= int(part) <= 255 for part in v.split('.') if part.isdigit()
        ):
            raise ValueError(f"Invalid IP address: {v}")
        return v


# ==============================================================================
# Camera Configuration
# ==============================================================================

class StreamProfile(BaseModel):
    """Camera stream profile."""
    width: int = Field(ge=424, le=1920)
    height: int = Field(ge=240, le=1080)
    fps: int = Field(ge=6, le=90)
    format: str
    
    @model_validator(mode='after')
    def validate_resolution(self) -> 'StreamProfile':
        """Validate resolution is supported by D435i."""
        valid_resolutions = {
            (424, 240), (480, 270), (640, 360), (640, 480),
            (848, 480), (1280, 720), (1920, 1080)
        }
        if (self.width, self.height) not in valid_resolutions:
            raise ValueError(
                f"Resolution {self.width}x{self.height} not supported by D435i"
            )
        return self


class CameraControls(BaseModel):
    """Camera control parameters."""
    auto_exposure: bool = True
    exposure: int = Field(default=8500, ge=1, le=200000)
    gain: int = Field(default=64, ge=16, le=248)
    white_balance: int = Field(default=4600, ge=0, le=10000)
    backlight_compensation: bool = False
    brightness: int = Field(default=0, ge=-64, le=64)
    contrast: int = Field(default=50, ge=0, le=100)
    gamma: int = Field(default=300, ge=100, le=500)
    hue: int = Field(default=0, ge=-180, le=180)
    saturation: int = Field(default=64, ge=0, le=100)
    sharpness: int = Field(default=50, ge=0, le=100)


class DepthFilterConfig(BaseModel):
    """Depth post-processing filter configuration."""
    enabled: bool
    magnitude: Optional[int] = None
    smooth_alpha: Optional[float] = None
    smooth_delta: Optional[int] = None
    hole_fill: Optional[int] = None
    persistence: Optional[int] = None
    mode: Optional[int] = None


class DepthFilters(BaseModel):
    """Depth filtering configuration."""
    decimation: DepthFilterConfig = DepthFilterConfig(enabled=False, magnitude=2)
    spatial: DepthFilterConfig = DepthFilterConfig(
        enabled=True, magnitude=2, smooth_alpha=0.5, smooth_delta=20, hole_fill=0
    )
    temporal: DepthFilterConfig = DepthFilterConfig(
        enabled=True, smooth_alpha=0.4, smooth_delta=20, persistence=3
    )
    hole_filling: DepthFilterConfig = DepthFilterConfig(enabled=True, mode=1)
    disparity: DepthFilterConfig = DepthFilterConfig(enabled=False)


class DepthConfig(BaseModel):
    """Depth processing configuration."""
    visual_preset: str = Field(default="High Accuracy")
    filters: DepthFilters = DepthFilters()
    align_to: Literal["color", "depth", "none"] = "color"
    min_distance: float = Field(default=0.3, ge=0.1, le=1.0)
    max_distance: float = Field(default=10.0, ge=1.0, le=16.0)


class CameraStreams(BaseModel):
    """Camera stream enablement."""
    color: bool = True
    depth: bool = True
    infrared: bool = True
    infrared_stereo: bool = True


class CameraConfig(BaseModel):
    """RealSense D435i camera configuration."""
    model: str = "D435i"
    serial: str = Field(default="", description="Camera serial number")
    streams: CameraStreams = CameraStreams()
    color_profile: StreamProfile
    depth_profile: StreamProfile
    infrared_profile: StreamProfile
    infrared_stereo_profile: StreamProfile
    controls: CameraControls = CameraControls()
    depth: DepthConfig = DepthConfig()


# ==============================================================================
# IMU Configuration
# ==============================================================================

class IMUAccelConfig(BaseModel):
    """IMU accelerometer configuration."""
    enabled: bool = True
    fps: Literal[63, 250] = 250


class IMUGyroConfig(BaseModel):
    """IMU gyroscope configuration."""
    enabled: bool = True
    fps: Literal[200, 400] = 200


class IMUTransport(BaseModel):
    """IMU transport configuration."""
    protocol: TransportProtocol = TransportProtocol.UDP
    port: int = Field(default=5050, ge=1024, le=65535)


class IMUConfig(BaseModel):
    """IMU configuration."""
    enabled: bool = True
    accel: IMUAccelConfig = IMUAccelConfig()
    gyro: IMUGyroConfig = IMUGyroConfig()
    publish_rate: float = Field(default=200.0, gt=0, le=400)
    frame_id: str = "camera_imu_optical_frame"
    transport: IMUTransport = IMUTransport()


# ==============================================================================
# GStreamer Configuration
# ==============================================================================

class PipelineConfig(BaseModel):
    """GStreamer pipeline control."""
    async_handling: bool = True
    message_forward: bool = True


class NVH264EncConfig(BaseModel):
    """NVIDIA hardware encoder configuration."""
    preset: str = Field(default="low-latency-hq")
    rc_mode: str = Field(default="cbr-ld-hq")
    gop_size: int = Field(default=30, ge=-1)
    bframes: int = Field(default=0, ge=0, le=4)
    aud: bool = True
    insert_sps_pps: bool = True
    insert_vui: bool = True
    qp_min: int = Field(default=20, ge=0, le=51)
    qp_max: int = Field(default=40, ge=0, le=51)
    qp_const: int = Field(default=-1, ge=-1, le=51)
    spatial_aq: bool = True
    temporal_aq: bool = False


class X264EncConfig(BaseModel):
    """x264 software encoder configuration."""
    tune: str = Field(default="zerolatency")
    speed_preset: str = Field(default="ultrafast")
    pass_: str = Field(default="cbr", alias="pass")
    key_int_max: int = Field(default=30, ge=0)
    bframes: int = Field(default=0, ge=0, le=16)
    aud: bool = True
    byte_stream: bool = True
    sliced_threads: bool = True
    threads: int = Field(default=4, ge=1, le=32)
    option_string: str = "sliced-threads=true:sync-lookahead=0"


class EncoderConfig(BaseModel):
    """Video encoder configuration."""
    nvh264enc: NVH264EncConfig = NVH264EncConfig()
    x264enc: X264EncConfig = X264EncConfig()


class RTPPayloaderConfig(BaseModel):
    """RTP payloader configuration."""
    mtu: int = Field(default=1400, ge=576, le=9000)
    config_interval: int = Field(default=1, ge=-1)
    aggregate_mode: str = Field(default="zero-latency")
    pt: int = Field(default=96, ge=96, le=127)


class RTPDepayloaderConfig(BaseModel):
    """RTP depayloader configuration."""
    wait_for_keyframe: bool = True
    request_keyframe: bool = True


class JitterBufferConfig(BaseModel):
    """RTP jitter buffer configuration."""
    latency: int = Field(default=50, ge=0, le=2000)
    drop_on_latency: bool = False
    do_lost: bool = True
    do_retransmission: bool = False
    max_dropout_time: int = Field(default=3000, ge=0)
    max_misorder_time: int = Field(default=100, ge=0)
    rtx_delay: int = Field(default=20, ge=0)
    mode: int = Field(default=1, ge=0, le=4)


class QueueConfig(BaseModel):
    """GStreamer queue configuration."""
    max_size_buffers: int = Field(default=3, ge=0)
    max_size_bytes: int = Field(default=0, ge=0)
    max_size_time: int = Field(default=0, ge=0)
    leaky: Literal["no", "upstream", "downstream"] = "downstream"
    silent: bool = True


class VideoConvertConfig(BaseModel):
    """Video converter configuration."""
    n_threads: int = Field(default=4, ge=1, le=32)
    dither: Literal["none", "verterr", "floyd-steinberg", "sierra-lite", "bayer"] = "none"


class VideoScaleConfig(BaseModel):
    """Video scaler configuration."""
    method: Literal["nearest", "bilinear", "bicubic", "lanczos"] = "bilinear"
    add_borders: bool = False


class AppSinkConfig(BaseModel):
    """App sink configuration."""
    emit_signals: bool = True
    sync: bool = False
    max_buffers: int = Field(default=3, ge=1)
    drop: bool = True


class AppSrcConfig(BaseModel):
    """App source configuration."""
    format: str = "time"
    is_live: bool = True
    do_timestamp: bool = True
    min_latency: int = Field(default=0, ge=0)
    max_latency: int = Field(default=100000000, ge=0)
    block: bool = False
    max_bytes: int = Field(default=0, ge=0)


class GStreamerElements(BaseModel):
    """GStreamer element configurations."""
    encoder: EncoderConfig = EncoderConfig()
    rtph264pay: RTPPayloaderConfig = RTPPayloaderConfig()
    rtph264depay: RTPDepayloaderConfig = RTPDepayloaderConfig()
    rtpjitterbuffer: JitterBufferConfig = JitterBufferConfig()
    queue: QueueConfig = QueueConfig()
    videoconvert: VideoConvertConfig = VideoConvertConfig()
    videoscale: VideoScaleConfig = VideoScaleConfig()
    appsink: AppSinkConfig = AppSinkConfig()
    appsrc: AppSrcConfig = AppSrcConfig()


class GStreamerConfig(BaseModel):
    """GStreamer pipeline configuration."""
    pipeline: PipelineConfig = PipelineConfig()
    elements: GStreamerElements = GStreamerElements()


# ==============================================================================
# Stream Presets
# ==============================================================================

class StreamConfig(BaseModel):
    """Individual stream configuration."""
    enabled: bool = True
    port: Union[int, List[int]]
    encoding: str = "h264"
    bitrate: int = Field(gt=0, le=50000)
    encoder: EncoderType = EncoderType.NVENC
    caps: str
    rtp_payload_type: Union[int, List[int]]
    
    @field_validator('port')
    @classmethod
    def validate_port(cls, v: Union[int, List[int]]) -> Union[int, List[int]]:
        """Validate port number(s)."""
        if isinstance(v, int):
            if not (1024 <= v <= 65535):
                raise ValueError(f"Port must be between 1024 and 65535, got {v}")
        elif isinstance(v, list):
            for port in v:
                if not (1024 <= port <= 65535):
                    raise ValueError(f"Port must be between 1024 and 65535, got {port}")
        return v
    
    @field_validator('rtp_payload_type')
    @classmethod
    def validate_payload_type(cls, v: Union[int, List[int]]) -> Union[int, List[int]]:
        """Validate RTP payload type(s)."""
        if isinstance(v, int):
            if not (96 <= v <= 127):
                raise ValueError(f"RTP payload type must be between 96 and 127, got {v}")
        elif isinstance(v, list):
            for pt in v:
                if not (96 <= pt <= 127):
                    raise ValueError(f"RTP payload type must be between 96 and 127, got {pt}")
        return v


class PresetStreams(BaseModel):
    """Preset stream definitions."""
    color: StreamConfig
    depth: StreamConfig
    infrared_stereo: Optional[StreamConfig] = None


class StreamPreset(BaseModel):
    """Stream preset configuration."""
    description: str
    color: StreamConfig
    depth: StreamConfig
    infrared_stereo: Optional[StreamConfig] = None


# ==============================================================================
# System Configuration
# ==============================================================================

class ThreadConfig(BaseModel):
    """Threading configuration."""
    gstreamer_workers: int = Field(default=4, ge=1, le=32)
    encoding_threads: int = Field(default=4, ge=1, le=32)
    camera_callbacks: int = Field(default=2, ge=1, le=8)


class TimingConfig(BaseModel):
    """Timing configuration."""
    pipeline_startup_delay: float = Field(default=2.0, ge=0, le=30)
    stream_sync_timeout: float = Field(default=5.0, ge=1, le=60)
    shutdown_timeout: float = Field(default=10.0, ge=1, le=60)


class BufferConfig(BaseModel):
    """Buffer management configuration."""
    udp_send_buffer: int = Field(default=30000000, ge=1000000)
    udp_recv_buffer: int = Field(default=30000000, ge=1000000)
    camera_queue_size: int = Field(default=4, ge=1, le=16)


class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    gstreamer_debug: str = Field(default="2", pattern=r"^[0-9]$")
    log_to_file: bool = False
    log_file: str = "/tmp/realsense_stream.log"


class ErrorHandlingConfig(BaseModel):
    """Error handling configuration."""
    max_consecutive_errors: int = Field(default=5, ge=1, le=100)
    error_cooldown: float = Field(default=10.0, ge=0)
    auto_restart: bool = True
    restart_delay: float = Field(default=5.0, ge=0)


class SystemConfig(BaseModel):
    """System-level configuration."""
    threads: ThreadConfig = ThreadConfig()
    timing: TimingConfig = TimingConfig()
    buffers: BufferConfig = BufferConfig()
    logging: LoggingConfig = LoggingConfig()
    error_handling: ErrorHandlingConfig = ErrorHandlingConfig()


# ==============================================================================
# ROS2 Configuration
# ==============================================================================

class ROS2QoS(BaseModel):
    """ROS2 QoS profile."""
    reliability: QoSReliability = QoSReliability.RELIABLE
    durability: Literal["volatile", "transient_local"] = "volatile"
    history: Literal["keep_last", "keep_all"] = "keep_last"
    depth: int = Field(default=10, ge=1, le=100)


class ROS2Topics(BaseModel):
    """ROS2 topic names."""
    color_image: str = "color/image_raw"
    color_info: str = "color/camera_info"
    depth_image: str = "depth/image_rect_raw"
    depth_info: str = "depth/camera_info"
    infrared_image: str = "infra/image_raw"
    infrared_info: str = "infra/camera_info"
    imu: str = "imu/data"


class ROS2Frames(BaseModel):
    """ROS2 frame IDs."""
    camera_link: str = "camera_link"
    color_optical: str = "camera_color_optical_frame"
    depth_optical: str = "camera_depth_optical_frame"
    infra_optical: str = "camera_infra_optical_frame"
    imu_optical: str = "camera_imu_optical_frame"


class ROS2Config(BaseModel):
    """ROS2 integration configuration."""
    enabled: bool = True
    node_name: str = "realsense_streamer"
    namespace: str = "camera"
    qos: ROS2QoS = ROS2QoS()
    topics: ROS2Topics = ROS2Topics()
    frames: ROS2Frames = ROS2Frames()


# ==============================================================================
# Root Configuration
# ==============================================================================

class RSConfig(BaseModel):
    """Root RealSense configuration."""
    platform: PlatformConfig
    network: NetworkConfig
    camera: CameraConfig
    imu: IMUConfig = IMUConfig()
    gstreamer: GStreamerConfig = GStreamerConfig()
    presets: Dict[str, StreamPreset]
    system: SystemConfig = SystemConfig()
    ros2: ROS2Config = ROS2Config()
    
    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "RSConfig":
        """Load configuration from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
    
    def to_yaml(self, path: Union[str, Path]) -> None:
        """Save configuration to YAML file."""
        with open(path, "w") as f:
            yaml.safe_dump(
                self.model_dump(by_alias=True, exclude_none=True),
                f,
                default_flow_style=False,
                sort_keys=False,
            )
    
    def get_encoder_for_platform(self) -> EncoderType:
        """Get optimal encoder for current platform."""
        if self.platform.type == PlatformType.JETSON and self.platform.nvenc_available:
            return EncoderType.NVENC
        return EncoderType.X264
    
    def get_encoder_params(self, encoder: Optional[EncoderType] = None) -> Dict[str, Any]:
        """Get encoder-specific parameters."""
        enc = encoder or self.get_encoder_for_platform()
        
        if enc == EncoderType.NVENC:
            cfg = self.gstreamer.elements.encoder.nvh264enc
            return {
                "name": "nvh264enc",
                "preset": cfg.preset,
                "rc-mode": cfg.rc_mode,
                "gop-size": cfg.gop_size,
                "bframes": cfg.bframes,
                "aud": cfg.aud,
                "insert-sps-pps": cfg.insert_sps_pps,
                "insert-vui": cfg.insert_vui,
                "qp-min": cfg.qp_min,
                "qp-max": cfg.qp_max,
                "spatial-aq": cfg.spatial_aq,
                "temporal-aq": cfg.temporal_aq,
            }
        else:  # X264
            cfg = self.gstreamer.elements.encoder.x264enc
            return {
                "name": "x264enc",
                "tune": cfg.tune,
                "speed-preset": cfg.speed_preset,
                "pass": cfg.pass_,
                "key-int-max": cfg.key_int_max,
                "bframes": cfg.bframes,
                "aud": cfg.aud,
                "byte-stream": cfg.byte_stream,
                "sliced-threads": cfg.sliced_threads,
                "threads": cfg.threads,
                "option-string": cfg.option_string,
            }
    
    def build_gst_caps(
        self,
        format: str,
        width: int,
        height: int,
        fps: int,
        additional: Optional[str] = None
    ) -> str:
        """Build GStreamer caps string."""
        caps = f"video/x-raw,format={format},width={width},height={height},framerate={fps}/1"
        if additional:
            caps += f",{additional}"
        return caps
    
    def build_rtp_caps(self, payload_type: int) -> str:
        """Build RTP caps string."""
        return (
            f"application/x-rtp,media=video,clock-rate=90000,"
            f"encoding-name=H264,payload={payload_type}"
        )


# ==============================================================================
# Configuration Manager
# ==============================================================================

class ConfigManager:
    """High-level configuration manager with convenience methods."""
    
    def __init__(self, config_path: Union[str, Path] = "config.yaml"):
        """Initialize configuration manager.
        
        Args:
            config_path: Path to YAML configuration file
        """
        self.config = RSConfig.from_yaml(config_path)
        self.config_path = Path(config_path)
    
    # ========================================================================
    # Quick Access Properties
    # ========================================================================
    @property
    def platform(self) -> PlatformConfig:
        """Platform configuration."""
        return self.config.platform
    
    @property
    def network(self) -> NetworkConfig:
        """Network configuration."""
        return self.config.network
    
    @property
    def camera(self) -> CameraConfig:
        """Camera configuration."""
        return self.config.camera
    
    @property
    def imu(self) -> IMUConfig:
        """IMU configuration."""
        return self.config.imu
    
    @property
    def gstreamer(self) -> GStreamerConfig:
        """GStreamer configuration."""
        return self.config.gstreamer
    
    @property
    def system(self) -> SystemConfig:
        """System configuration."""
        return self.config.system
    
    # ========================================================================
    # Preset Management
    # ========================================================================
    def get_preset(self, name: str) -> Optional[StreamPreset]:
        """Get stream preset by name."""
        return self.config.presets.get(name)
    
    def list_presets(self) -> List[str]:
        """List available preset names."""
        return list(self.config.presets.keys())
    
    def get_stream_config(self, preset: str, stream: str) -> Optional[StreamConfig]:
        """Get stream configuration from preset."""
        preset_cfg = self.get_preset(preset)
        if not preset_cfg:
            return None
        return getattr(preset_cfg, stream, None)
    
    # ========================================================================
    # Pipeline Building
    # ========================================================================
    def build_sender_pipeline(
        self,
        stream_type: str,
        preset: str = "balanced"
    ) -> str:
        """Build GStreamer sender pipeline string.
        
        Args:
            stream_type: Type of stream ("color", "depth", "infrared_stereo")
            preset: Preset name to use
            
        Returns:
            Complete GStreamer pipeline string
        """
        stream = self.get_stream_config(preset, stream_type)
        if not stream:
            raise ValueError(f"Stream {stream_type} not found in preset {preset}")
        
        # Get encoder
        encoder_params = self.config.get_encoder_params(stream.encoder)
        encoder_str = " ".join(f"{k}={v}" for k, v in encoder_params.items())
        
        # Get payloader params
        pay_cfg = self.gstreamer.elements.rtph264pay
        
        # Get queue params
        queue_cfg = self.gstreamer.elements.queue
        
        # Build pipeline
        pipeline = (
            f"appsrc name=src format=time is-live=true do-timestamp=true ! "
            f"{stream.caps} ! "
            f"videoconvert n-threads={self.gstreamer.elements.videoconvert.n_threads} ! "
            f"{encoder_str} bitrate={stream.bitrate} ! "
            f"h264parse ! "
            f"rtph264pay name=pay mtu={pay_cfg.mtu} "
            f"config-interval={pay_cfg.config_interval} "
            f"aggregate-mode={pay_cfg.aggregate_mode} "
            f"pt={stream.rtp_payload_type} ! "
            f"udpsink host={self.network.server_ip} port={stream.port} "
            f"buffer-size={self.system.buffers.udp_send_buffer}"
        )
        
        return pipeline
    
    def build_receiver_pipeline(
        self,
        stream_type: str,
        preset: str = "balanced"
    ) -> str:
        """Build GStreamer receiver pipeline string.
        
        Args:
            stream_type: Type of stream ("color", "depth", "infrared_stereo")
            preset: Preset name to use
            
        Returns:
            Complete GStreamer pipeline string
        """
        stream = self.get_stream_config(preset, stream_type)
        if not stream:
            raise ValueError(f"Stream {stream_type} not found in preset {preset}")
        
        # Get jitter buffer params
        jb_cfg = self.gstreamer.elements.rtpjitterbuffer
        
        # Get queue params
        queue_cfg = self.gstreamer.elements.queue
        
        # Build RTP caps
        rtp_caps = self.config.build_rtp_caps(
            stream.rtp_payload_type if isinstance(stream.rtp_payload_type, int) 
            else stream.rtp_payload_type[0]
        )
        
        # Build pipeline
        pipeline = (
            f"udpsrc port={stream.port if isinstance(stream.port, int) else stream.port[0]} "
            f"buffer-size={self.system.buffers.udp_recv_buffer} ! "
            f"{rtp_caps} ! "
            f"rtpjitterbuffer latency={jb_cfg.latency} "
            f"drop-on-latency={str(jb_cfg.drop_on_latency).lower()} "
            f"do-lost={str(jb_cfg.do_lost).lower()} "
            f"do-retransmission={str(jb_cfg.do_retransmission).lower()} ! "
            f"rtph264depay ! "
            f"h264parse ! "
            f"avdec_h264 ! "
            f"videoconvert ! "
            f"queue max-size-buffers={queue_cfg.max_size_buffers} "
            f"leaky={queue_cfg.leaky} ! "
            f"appsink name=sink emit-signals=true sync=false max-buffers={self.gstreamer.elements.appsink.max_buffers} drop=true"
        )
        
        return pipeline
    
    # ========================================================================
    # Camera Utilities
    # ========================================================================
    def get_camera_resolution(self, stream_type: str = "color") -> tuple[int, int]:
        """Get camera resolution for stream type."""
        profile_map = {
            "color": self.camera.color_profile,
            "depth": self.camera.depth_profile,
            "infrared": self.camera.infrared_profile,
            "infrared_stereo": self.camera.infrared_stereo_profile,
        }
        profile = profile_map.get(stream_type)
        if not profile:
            raise ValueError(f"Unknown stream type: {stream_type}")
        return profile.width, profile.height
    
    def get_camera_fps(self, stream_type: str = "color") -> int:
        """Get camera FPS for stream type."""
        profile_map = {
            "color": self.camera.color_profile,
            "depth": self.camera.depth_profile,
            "infrared": self.camera.infrared_profile,
            "infrared_stereo": self.camera.infrared_stereo_profile,
        }
        profile = profile_map.get(stream_type)
        if not profile:
            raise ValueError(f"Unknown stream type: {stream_type}")
        return profile.fps
    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    def save(self, path: Optional[Union[str, Path]] = None) -> None:
        """Save configuration to YAML file."""
        save_path = path or self.config_path
        self.config.to_yaml(save_path)
    
    def reload(self) -> None:
        """Reload configuration from file."""
        self.config = RSConfig.from_yaml(self.config_path)
    
    def validate(self) -> bool:
        """Validate current configuration."""
        try:
            self.config.model_validate(self.config.model_dump())
            return True
        except Exception as e:
            print(f"Validation error: {e}")
            return False
    
    def __repr__(self) -> str:
        return (
            f"ConfigManager(\n"
            f"  platform={self.platform.type.value}:{self.platform.device},\n"
            f"  presets={self.list_presets()},\n"
            f"  config_path={self.config_path}\n"
            f")"
        )


# ==============================================================================
# Usage Examples
# ==============================================================================

if __name__ == "__main__":
    import sys
    
    # Initialize
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = ConfigManager(config_file)
    
    print("=" * 80)
    print("Configuration Manager Initialized")
    print("=" * 80)
    print(config)
    print()
    
    # Platform info
    print(f"Platform: {config.platform.type.value}")
    print(f"Device: {config.platform.device}")
    print(f"NVENC Available: {config.platform.nvenc_available}")
    print()
    
    # Network info
    print(f"Server IP: {config.network.server_ip}")
    print(f"Transport: {config.network.transport.protocol.value}")
    print(f"MTU: {config.network.transport.mtu}")
    print()
    
    # Camera info
    width, height = config.get_camera_resolution("color")
    fps = config.get_camera_fps("color")
    print(f"Color Resolution: {width}x{height} @ {fps}fps")
    print()
    
    # Encoder info
    encoder = config.config.get_encoder_for_platform()
    print(f"Selected Encoder: {encoder.value}")
    encoder_params = config.config.get_encoder_params()
    print(f"Encoder Parameters: {encoder_params}")
    print()
    
    # Pipeline examples
    print("=" * 80)
    print("Pipeline Examples")
    print("=" * 80)
    
    try:
        sender_pipeline = config.build_sender_pipeline("color", "balanced")
        print("\nSender Pipeline (Color):")
        print(sender_pipeline)
        print()
        
        receiver_pipeline = config.build_receiver_pipeline("depth", "balanced")
        print("Receiver Pipeline (Depth):")
        print(receiver_pipeline)
        print()
    except Exception as e:
        print(f"Error building pipelines: {e}")
    
    # Validation
    print("=" * 80)
    is_valid = config.validate()
    print(f"Configuration Valid: {is_valid}")
    print("=" * 80)