#!/usr/bin/env python3
"""
RealSense Camera Info Parser

Parses rs-enumerate-devices output and generates ROS 2 camera_info configuration files.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml


class RealSenseCameraInfoParser:
    """Parser for RealSense camera intrinsic parameters."""

    def __init__(self):
        self.camera_data: dict = {
            "depth": {},
            "color": {},
            "infrared": {},  # Merges Infrared 1 and Infrared 2
        }

    def run_rs_enumerate(self):
        """Execute rs-enumerate-devices -c command."""
        try:
            result = subprocess.run(
                ["rs-enumerate-devices", "-c"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            print(f"Command execution failed: {e}")
            return None
        except FileNotFoundError:
            print(
                "rs-enumerate-devices command not found. "
                "Please ensure RealSense SDK is installed."
            )
            return None

    def parse_intrinsics_section(self, output):
        """Parse the Intrinsic Parameters section."""
        lines = output.split("\n")

        # Find the start of Intrinsic Parameters section
        start_idx = -1
        for idx, line in enumerate(lines):
            if "Intrinsic Parameters:" in line:
                start_idx = idx
                break

        if start_idx == -1:
            print("Intrinsic Parameters section not found")
            return

        # Find the end position (Motion Intrinsic or Extrinsic)
        end_idx = len(lines)
        for idx in range(start_idx, len(lines)):
            if "Motion Intrinsic" in lines[idx] or "Extrinsic Parameters:" in lines[idx]:
                end_idx = idx
                break

        # Parse each intrinsic block
        i = start_idx + 1
        while i < end_idx:
            line = lines[i].strip()

            # Match Intrinsic of "StreamType" / Resolution / {Format}
            match = re.match(r'Intrinsic of "([^"]+)"\s+/\s+(\d+x\d+)', line)
            if match:
                stream_type = match.group(1)  # e.g., "Depth", "Color", "Infrared 1"
                resolution = match.group(2)  # e.g., "640x480"

                # Parse detailed data for this intrinsic block
                intrinsic_data = self.parse_single_intrinsic(lines, i)

                # Store by stream type
                if stream_type == "Depth":
                    self.camera_data["depth"][resolution] = intrinsic_data
                elif stream_type == "Color":
                    self.camera_data["color"][resolution] = intrinsic_data
                elif (
                    stream_type.startswith("Infrared")
                    and resolution not in self.camera_data["infrared"]
                ):
                    self.camera_data["infrared"][resolution] = intrinsic_data

            i += 1

    def parse_single_intrinsic(self, lines: list[str], start_idx: int) -> dict[str, Any]:
        """Parse a single intrinsic block."""
        intrinsic_data: dict = {
            "width": 0,
            "height": 0,
            "ppx": 0.0,
            "ppy": 0.0,
            "fx": 0.0,
            "fy": 0.0,
            "distortion_model": "",
            "distortion_coeffs": [],
        }

        # Search within approximately 10-15 lines from start
        for i in range(start_idx, min(start_idx + 20, len(lines))):
            line = lines[i].strip()

            # Stop if encountering next Intrinsic block or empty region
            if i > start_idx and (
                line.startswith("Intrinsic of")
                or line.startswith("Motion Intrinsic")
                or line.startswith("Extrinsic")
            ):
                break

            # Width
            if line.startswith("Width:"):
                match = re.search(r"Width:\s+(\d+)", line)
                if match:
                    intrinsic_data["width"] = int(match.group(1))

            # Height
            elif line.startswith("Height:"):
                match = re.search(r"Height:\s+(\d+)", line)
                if match:
                    intrinsic_data["height"] = int(match.group(1))

            # PPX (principal point x)
            elif line.startswith("PPX:"):
                match = re.search(r"PPX:\s+([\d.]+)", line)
                if match:
                    intrinsic_data["ppx"] = float(match.group(1))

            # PPY (principal point y)
            elif line.startswith("PPY:"):
                match = re.search(r"PPY:\s+([\d.]+)", line)
                if match:
                    intrinsic_data["ppy"] = float(match.group(1))

            # Fx (focal length x)
            elif line.startswith("Fx:"):
                match = re.search(r"Fx:\s+([\d.]+)", line)
                if match:
                    intrinsic_data["fx"] = float(match.group(1))

            # Fy (focal length y)
            elif line.startswith("Fy:"):
                match = re.search(r"Fy:\s+([\d.]+)", line)
                if match:
                    intrinsic_data["fy"] = float(match.group(1))

            # Distortion model
            elif line.startswith("Distortion:"):
                match = re.search(r"Distortion:\s+(.+)", line)
                if match:
                    model = match.group(1).strip()
                    # Convert to ROS 2 format
                    if "Brown Conrady" in model:
                        intrinsic_data["distortion_model"] = "plumb_bob"
                    elif "Inverse Brown Conrady" in model:
                        intrinsic_data["distortion_model"] = "rational_polynomial"
                    else:
                        intrinsic_data["distortion_model"] = "plumb_bob"

            # Distortion coefficients
            elif line.startswith("Coeffs:"):
                # Extract all numbers (including scientific notation)
                coeffs_str = line.replace("Coeffs:", "").strip()
                coeffs = re.findall(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", coeffs_str)
                intrinsic_data["distortion_coeffs"] = [float(c) for c in coeffs[:5]]
                # Ensure 5 coefficients
                while len(intrinsic_data["distortion_coeffs"]) < 5:
                    intrinsic_data["distortion_coeffs"].append(0.0)

        return intrinsic_data

    def generate_ros2_camera_info(self, stream_type, resolution, intrinsic_data):
        """Generate ROS 2 camera_info format configuration."""
        width = intrinsic_data["width"]
        height = intrinsic_data["height"]
        fx = intrinsic_data["fx"]
        fy = intrinsic_data["fy"]
        cx = intrinsic_data["ppx"]
        cy = intrinsic_data["ppy"]
        distortion_model = intrinsic_data["distortion_model"]
        distortion_coeffs = intrinsic_data["distortion_coeffs"]

        # ROS 2 camera_info format
        camera_info = {
            "image_width": width,
            "image_height": height,
            "camera_name": f"realsense_{stream_type}_{resolution}",
            "distortion_model": distortion_model,
            "distortion_coefficients": {"rows": 1, "cols": 5, "data": distortion_coeffs},
            "camera_matrix": {
                "rows": 3,
                "cols": 3,
                "data": [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0],
            },
            "rectification_matrix": {
                "rows": 3,
                "cols": 3,
                "data": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            },
            "projection_matrix": {
                "rows": 3,
                "cols": 4,
                "data": [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0],
            },
        }

        return camera_info

    def save_to_files(self, output_dir="camera_info"):
        """
        Save information to files.

        Includes both overview files and individual resolution files.
        """
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        stream_names = {
            "depth": "depth_camera_info.yaml",
            "color": "color_camera_info.yaml",
            "infrared": "infrared_camera_info.yaml",
        }

        for stream_type, filename in stream_names.items():
            resolutions = self.camera_data[stream_type]

            if not resolutions:
                print(f"⚠  Warning: No data found for {stream_type} stream")
                continue

            # 1. Save overview file containing all resolutions
            filepath = output_path / filename
            all_configs = {}
            for resolution, intrinsic_data in sorted(resolutions.items()):
                camera_info = self.generate_ros2_camera_info(
                    stream_type, resolution, intrinsic_data
                )
                all_configs[resolution] = camera_info

            with open(filepath, "w", encoding="utf-8") as f:
                yaml.dump(
                    all_configs,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )

            print(f"✓ Saved {stream_type} overview to {filepath}")
            print(
                f"  Contains {len(resolutions)} resolutions: "
                f"{', '.join(sorted(resolutions.keys()))}"
            )

            # 2. Generate individual camera_info files for each resolution (for gscam)
            for resolution, intrinsic_data in resolutions.items():
                camera_info = self.generate_ros2_camera_info(
                    stream_type, resolution, intrinsic_data
                )

                # Generate individual filename, e.g., depth_camera_424x240.yaml
                single_filename = f"{stream_type}_camera_{resolution}.yaml"
                single_filepath = output_path / single_filename

                with open(single_filepath, "w", encoding="utf-8") as f:
                    yaml.dump(
                        camera_info,
                        f,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )

            print(f"  ✓ Generated {len(resolutions)} individual resolution config files")
            print()


def main():
    """Main entry point."""
    print("=" * 70)
    print("RealSense Camera Info Parser for ROS 2")
    print("=" * 70)
    print()

    parser = RealSenseCameraInfoParser()

    # Method 1: Execute rs-enumerate-devices command
    print("Running rs-enumerate-devices -c ...")
    output = parser.run_rs_enumerate()

    # Method 2: If command fails, try reading from file
    if not output:
        print("\nAttempting to read from file...")
        try:
            with open("rs_output.txt", encoding="utf-8") as f:
                output = f.read()
            print("✓ Successfully read data from rs_output.txt")
        except FileNotFoundError:
            print("❌ Unable to obtain camera information")
            print("\nTip: You can save the output of 'rs-enumerate-devices -c'")
            print("     to rs_output.txt and run this script again")
            return
    else:
        print("✓ Successfully executed command and obtained camera information")

    print()

    # Parse intrinsic data
    print("Parsing camera parameters...")
    parser.parse_intrinsics_section(output)

    # Display parsing summary
    print("\nParsing Summary:")
    print(f"  - Depth:    {len(parser.camera_data['depth'])} resolutions")
    print(f"  - Color:    {len(parser.camera_data['color'])} resolutions")
    print(f"  - Infrared: {len(parser.camera_data['infrared'])} resolutions")
    print()

    # Save to files
    print("Generating ROS 2 camera_info configuration files...")
    print()
    parser.save_to_files()

    print("=" * 70)
    print("✓ Complete!")
    print("=" * 70)
    print("\nGenerated files are located in the camera_info/ directory:")
    print("  - depth_camera_info.yaml")
    print("  - color_camera_info.yaml")
    print("  - infrared_camera_info.yaml")
    print("\nYou can use these configurations in ROS 2 launch files:")
    print("  camera_info_url: file://$(find-pkg-share pkg)/config/depth_camera_info.yaml")


if __name__ == "__main__":
    main()
