#!/usr/bin/env python3

import os
import subprocess
import logging
import zipfile
import tempfile
import shutil
import glob
import concurrent.futures
from pathlib import Path
from threading import Lock
import multiprocessing
import uuid

mount_lock = Lock()

def extract_apex_contents(apex_path, output_dir, apex_name):
    """Extract the contents of an APEX file."""
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="apex_extract_")

        # Extract the APEX file
        with zipfile.ZipFile(apex_path, 'r') as zip_ref:
            zip_ref.extractall(tmp_dir)

        # Find the image file
        img_files = list(Path(tmp_dir).glob("*.img"))
        if not img_files:
            img_files = list(Path(tmp_dir).glob("**/*.img"))

        if img_files:
            img_path = str(img_files[0])
            dest_path = os.path.join(output_dir, f"apex_payload_{apex_name}.img")
            shutil.copy(img_path, dest_path)
            shutil.rmtree(tmp_dir)
            return dest_path

        # No image file found - maybe it's a filesystem image directly
        if os.path.exists(os.path.join(tmp_dir, "apex_manifest.json")) or \
           os.path.exists(os.path.join(tmp_dir, "apex_manifest.pb")):
            # This looks like an extracted APEX filesystem
            dest_dir = os.path.join(output_dir, apex_name)
            with mount_lock:
                if os.path.exists(dest_dir):
                    shutil.rmtree(dest_dir)
                shutil.move(tmp_dir, dest_dir)
            return dest_dir

        shutil.rmtree(tmp_dir)
        return None
    except Exception as e:
        logging.debug(f"Failed to extract APEX contents from {apex_path}: {e}")
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        return None

def handle_capex(capex_path, work_dir, apex_name):
    """Handle a compressed APEX (.capex) file."""
    logging.debug(f"Processing compressed APEX: {capex_path}")

    basename = os.path.basename(capex_path).replace('.capex', '.apex')
    apex_path = os.path.join(work_dir, basename)

    try:
        with open(os.path.join(work_dir, ".apex_mounted"), "w") as f:
            f.write(apex_name)

        # Method 1: Extract as "original_apex"
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                subprocess.run(['unzip', '-p', capex_path, 'original_apex'],
                               stdout=open(apex_path, 'wb'), check=True, stderr=subprocess.PIPE)
                logging.debug(f"Extracted {capex_path} as original_apex")
                return apex_path
            except subprocess.CalledProcessError:
                pass

        # Method 2: Try to find the actual apex file inside
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                subprocess.run(['unzip', capex_path, '*.apex', '-d', temp_dir], check=True, stderr=subprocess.PIPE)
                apex_files = list(Path(temp_dir).glob("**/*.apex"))
                if apex_files:
                    shutil.copy(str(apex_files[0]), apex_path)
                    logging.debug(f"Extracted {capex_path} by finding .apex file inside")
                    return apex_path
            except subprocess.CalledProcessError:
                pass

        # Method 3: Just copy the .capex to .apex and try to use it directly
        shutil.copy(capex_path, apex_path)
        logging.debug(f"Copied {capex_path} to {apex_path} for direct use")
        return apex_path
    except Exception as e:
        logging.debug(f"Failed to handle compressed APEX {capex_path}: {e}")
        if os.path.exists(apex_path):
            os.unlink(apex_path)
        return None

def get_apex_name(apex_path):
    """Extract the package name from the APEX file path."""
    basename = os.path.basename(apex_path)
    # Remove extension (.apex or .capex)
    if basename.endswith('.capex'):
        return basename[:-6]
    elif basename.endswith('.apex'):
        return basename[:-5]
    return basename

def mount_apex_direct(apex_path, target_dir):
    """Extract and 'mount' the APEX by extraction."""
    apex_name = get_apex_name(apex_path)
    mount_point = os.path.join(target_dir, apex_name)
    work_dir = None

    with mount_lock:
        if os.path.exists(mount_point):
            if os.path.isdir(mount_point) and os.listdir(mount_point):
                logging.debug(f"{mount_point} already exists and is not empty, skipping")
                return True
            else:
                if os.path.isdir(mount_point):
                    os.rmdir(mount_point)
                else:
                    os.unlink(mount_point)
        os.makedirs(mount_point, exist_ok=True)

    # Handle .capex files
    if apex_path.endswith('.capex'):
        unique_id = str(uuid.uuid4())[:8]
        work_dir = tempfile.mkdtemp(prefix=f"apex_work_{unique_id}_")
        with open(os.path.join(target_dir, f".work_dir_{apex_name}"), "w") as f:
            f.write(work_dir)

        apex_path = handle_capex(apex_path, work_dir, apex_name)
        if not apex_path:
            if work_dir and os.path.exists(work_dir):
                shutil.rmtree(work_dir)
            return False

    result = extract_apex_contents(apex_path, target_dir, apex_name)

    if isinstance(result, str) and os.path.isdir(result):
        # If extract_apex_contents returned a directory path, it's already been extracted
        with mount_lock:
            if result != mount_point:
                if os.path.exists(mount_point):
                    shutil.rmtree(mount_point)
                shutil.move(result, mount_point)

        if work_dir:
            with open(os.path.join(target_dir, f".temp_dir_{apex_name}"), "w") as f:
                f.write(work_dir)

        logging.debug(f"Extracted APEX contents to {mount_point}")
        return True
    elif isinstance(result, str) and os.path.isfile(result):
        # If extract_apex_contents returned a file path, it's an image file
        logging.debug(f"Extracted image file {result}, attempting to mount it")

        try:
            # Create a loop device for the image file
            loop_dev = subprocess.check_output(
                ['losetup', '-f', '--show', result],
                text=True
            ).strip()

            logging.debug(f"Created loop device {loop_dev} for {result}")

            # Determine the filesystem type
            fs_type = None
            try:
                blkid_output = subprocess.check_output(['blkid', '-o', 'value', '-s', 'TYPE', loop_dev], text=True).strip()
                if blkid_output:
                    fs_type = blkid_output
            except subprocess.CalledProcessError:
                # Try common filesystem types
                fs_type = None

            mount_options = ['mount']
            if fs_type:
                mount_options.extend(['-t', fs_type])
            mount_options.extend(['-o', 'ro', loop_dev, mount_point])

            subprocess.run(mount_options, check=True)
            logging.debug(f"Mounted {loop_dev} on {mount_point}")

            with open(os.path.join(target_dir, f".loop_device_{apex_name}"), "w") as f:
                f.write(f"{loop_dev}\n{result}")

            if work_dir:
                with open(os.path.join(target_dir, f".temp_dir_{apex_name}"), "w") as f:
                    f.write(work_dir)

            return True
        except subprocess.CalledProcessError as e:
            logging.debug(f"Failed to mount {result}: {e}")
            # Clean up loop device if it was created
            try:
                subprocess.run(['losetup', '-d', loop_dev], check=False)
            except:
                pass

            # Clean up image file
            try:
                if os.path.exists(result):
                    os.unlink(result)
            except:
                pass

            if work_dir and os.path.exists(work_dir):
                shutil.rmtree(work_dir)

            return False

    # If we get here, extraction failed
    logging.debug(f"Failed to extract or mount {apex_path}")

    if work_dir and os.path.exists(work_dir):
        shutil.rmtree(work_dir)

    return False

def cleanup_post_mount(target_dir):
    """Clean up temporary files after successful mounts."""
    logging.debug("Performing post-mount cleanup...")

    img_files = list(Path(target_dir).glob("apex_payload_*.img"))
    for img_file in img_files:
        try:
            logging.debug(f"Removing image file {img_file}")
            os.unlink(str(img_file))
        except Exception as e:
            logging.debug(f"Failed to remove image file {img_file}: {e}")

    work_dir_files = list(Path(target_dir).glob(".work_dir_*"))
    for work_dir_file in work_dir_files:
        try:
            with open(str(work_dir_file), "r") as f:
                work_dir = f.read().strip()
            if os.path.exists(work_dir):
                logging.debug(f"Removing work directory {work_dir}")
                shutil.rmtree(work_dir)
            os.unlink(str(work_dir_file))
        except Exception as e:
            logging.debug(f"Failed to clean up work directory from {work_dir_file}: {e}")

    temp_dir_files = list(Path(target_dir).glob(".temp_dir_*"))
    for temp_dir_file in temp_dir_files:
        try:
            with open(str(temp_dir_file), "r") as f:
                temp_dir = f.read().strip()
            if os.path.exists(temp_dir):
                logging.debug(f"Removing temp directory {temp_dir}")
                shutil.rmtree(temp_dir)
            os.unlink(str(temp_dir_file))
        except Exception as e:
            logging.debug(f"Failed to clean up temp directory from {temp_dir_file}: {e}")

    cleanup_stray_temp_dirs("/tmp/apex_work_*")
    cleanup_stray_temp_dirs("/tmp/apex_extract_*")

    logging.debug("Post-mount cleanup completed!")

def unmount_apex(mount_point_info):
    """Unmount a single APEX mount point and clean up resources."""
    mount_point, target_dir = mount_point_info
    apex_name = os.path.basename(mount_point)

    loop_device_file = os.path.join(target_dir, f".loop_device_{apex_name}")
    temp_dir_file = os.path.join(target_dir, f".temp_dir_{apex_name}")

    loop_device = None
    img_file = None
    temp_dir = None

    # Check if we have saved loop device information
    if os.path.exists(loop_device_file):
        try:
            with open(loop_device_file, "r") as f:
                lines = f.read().strip().split('\n')
                if len(lines) >= 1:
                    loop_device = lines[0]
                if len(lines) >= 2:
                    img_file = lines[1]
            os.unlink(loop_device_file)
        except Exception as e:
            logging.debug(f"Failed to read loop device info from {loop_device_file}: {e}")

    if os.path.exists(temp_dir_file):
        try:
            with open(temp_dir_file, "r") as f:
                temp_dir = f.read().strip()
            os.unlink(temp_dir_file)
        except Exception as e:
            logging.debug(f"Failed to read temp directory info from {temp_dir_file}: {e}")

    # Try to unmount
    try:
        logging.debug(f"Unmounting {mount_point}")
        subprocess.run(['umount', mount_point], check=True)
    except subprocess.CalledProcessError as e:
        logging.debug(f"Failed to unmount {mount_point}: {e}")
        return False

    # Clean up loop device if we have it
    if loop_device:
        try:
            logging.debug(f"Detaching loop device {loop_device}")
            subprocess.run(['losetup', '-d', loop_device], check=True)
        except subprocess.CalledProcessError as e:
            logging.debug(f"Failed to detach loop device {loop_device}: {e}")

    # Clean up image file if we have it
    if img_file and os.path.exists(img_file):
        try:
            logging.debug(f"Removing image file {img_file}")
            os.unlink(img_file)
        except Exception as e:
            logging.debug(f"Failed to remove image file {img_file}: {e}")

    # Clean up temp directory if we have it
    if temp_dir and os.path.exists(temp_dir):
        try:
            logging.debug(f"Removing temp directory {temp_dir}")
            shutil.rmtree(temp_dir)
        except Exception as e:
            logging.debug(f"Failed to remove temp directory {temp_dir}: {e}")

    try:
        shutil.rmtree(mount_point)
    except Exception as e:
        logging.debug(f"Failed to remove mount point directory {mount_point}: {e}")
        return False

    return True

def cleanup_temporary_files(target_dir):
    """Clean up any temporary files and directories created during mounting."""
    work_dir_files = list(Path(target_dir).glob(".work_dir_*"))
    for work_dir_file in work_dir_files:
        try:
            with open(str(work_dir_file), "r") as f:
                work_dir = f.read().strip()
            if os.path.exists(work_dir):
                logging.debug(f"Removing work directory {work_dir}")
                shutil.rmtree(work_dir)
            os.unlink(str(work_dir_file))
        except Exception as e:
            logging.debug(f"Failed to clean up work directory from {work_dir_file}: {e}")

    temp_dir_files = list(Path(target_dir).glob(".temp_dir_*"))
    for temp_dir_file in temp_dir_files:
        try:
            with open(str(temp_dir_file), "r") as f:
                temp_dir = f.read().strip()
            if os.path.exists(temp_dir):
                logging.debug(f"Removing temp directory {temp_dir}")
                shutil.rmtree(temp_dir)
            os.unlink(str(temp_dir_file))
        except Exception as e:
            logging.debug(f"Failed to clean up temp directory from {temp_dir_file}: {e}")

    img_files = list(Path(target_dir).glob("apex_payload_*.img"))
    for img_file in img_files:
        try:
            logging.debug(f"Removing image file {img_file}")
            os.unlink(str(img_file))
        except Exception as e:
            logging.debug(f"Failed to remove image file {img_file}: {e}")

def cleanup_stray_temp_dirs(pattern="/tmp/apex_*"):
    """Clean up stray temporary directories created by this script."""
    try:
        temp_dirs = glob.glob(pattern)
        if not temp_dirs:
            logging.debug(f"No stray temporary directories found matching {pattern}")
            return 0

        count = 0
        for temp_dir in temp_dirs:
            if os.path.isdir(temp_dir):
                try:
                    logging.debug(f"Removing stray temporary directory {temp_dir}")
                    shutil.rmtree(temp_dir)
                    count += 1
                except Exception as e:
                    logging.debug(f"Failed to remove {temp_dir}: {e}")

        logging.debug(f"Cleaned up {count} stray temporary directories")
        return count
    except Exception as e:
        logging.debug(f"Error during cleanup of stray temp dirs: {e}")
        return 0

def cleanup_dot_files(target_dir):
    """Clean up all dot files after mounting."""
    dot_files = list(Path(target_dir).glob(".*"))
    count = 0
    for dot_file in dot_files:
        # Skip . and ..
        if str(dot_file.name) in ['.', '..']:
            continue
        try:
            if os.path.isdir(dot_file):
                shutil.rmtree(dot_file)
            else:
                os.unlink(dot_file)
            count += 1
        except Exception as e:
            logging.debug(f"Failed to remove dot file {dot_file}: {e}")
    logging.debug(f"Cleaned up {count} dot files")
    return count

def unmount_all_apexes(target_dir):
    """Unmount all APEXes in the target directory and clean up loop devices."""
    max_workers = multiprocessing.cpu_count()
    logging.debug(f"Using {max_workers} worker threads for unmounting")

    if not os.path.exists(target_dir):
        logging.debug(f"Target directory {target_dir} does not exist")
        return 0

    # Get all mounted filesystems
    try:
        mount_output = subprocess.check_output(['mount'], text=True)
    except subprocess.CalledProcessError:
        logging.debug("Failed to get mount information")
        return 0

    # Find all mount points under target_dir
    mount_points = []
    for mount_point in os.listdir(target_dir):
        # Skip hidden files that contain metadata
        if mount_point.startswith('.'):
            continue

        full_path = os.path.join(target_dir, mount_point)
        if os.path.isdir(full_path) and (full_path in mount_output or
                                         os.path.exists(os.path.join(target_dir, f".loop_device_{mount_point}"))):
            mount_points.append((full_path, target_dir))

    # Unmount in parallel
    unmounted_count = 0
    if mount_points:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(unmount_apex, mount_points))
            unmounted_count = sum(1 for r in results if r)

    extracted_dirs = []
    for item in os.listdir(target_dir):
        # Skip hidden files
        if item.startswith("."):
            continue
        full_path = os.path.join(target_dir, item)
        if os.path.isdir(full_path) and (full_path, target_dir) not in mount_points:
            extracted_dirs.append(full_path)

    # Remove extracted directories in parallel
    if extracted_dirs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            def remove_dir(dir_path):
                try:
                    logging.debug(f"Removing extracted directory {dir_path}")
                    shutil.rmtree(dir_path)
                    return True
                except Exception as e:
                    logging.debug(f"Failed to remove directory {dir_path}: {e}")
                    return False

            results = list(executor.map(remove_dir, extracted_dirs))
            unmounted_count += sum(1 for r in results if r)

    cleanup_temporary_files(target_dir)

    cleanup_dot_files(target_dir)

    # Check for any remaining loop devices that might be associated with our mounts
    try:
        loop_devices = subprocess.check_output(['losetup', '-l'], text=True)
        orphaned_loop_devs = []

        for line in loop_devices.splitlines()[1:]:  # Skip header line
            if target_dir in line:
                fields = line.split()
                if fields:
                    orphaned_loop_devs.append(fields[0])

        if orphaned_loop_devs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                def detach_loop(loop_dev):
                    try:
                        logging.debug(f"Detaching orphaned loop device {loop_dev}")
                        subprocess.run(['losetup', '-d', loop_dev], check=True)
                        return True
                    except subprocess.CalledProcessError as e:
                        logging.debug(f"Failed to detach orphaned loop device {loop_dev}: {e}")
                        return False

                list(executor.map(detach_loop, orphaned_loop_devs))
    except subprocess.CalledProcessError:
        logging.debug("Failed to list loop devices")

    cleanup_stray_temp_dirs("/tmp/apex_work_*")
    cleanup_stray_temp_dirs("/tmp/apex_extract_*")

    logging.debug(f"Unmounted and cleaned up {unmounted_count} APEX mount points")
    return unmounted_count

def mount_apexes(source_dir, target_dir, keep_files=False):
    """
    Mount APEX files from source_dir to target_dir.

    Args:
        source_dir (str): Directory containing APEX files
        target_dir (str): Directory where APEXes will be mounted
        keep_files (bool): If True, don't clean up image files and dot files after mounting

    Returns:
        int: Number of successfully processed APEX files
    """

    source_dir = os.path.abspath(source_dir)
    target_dir = os.path.abspath(target_dir)

    if not os.path.isdir(source_dir):
        logging.debug(f"Source directory {source_dir} does not exist")
        return 0

    logging.debug(f"Processing APEX files from {source_dir}")

    os.makedirs(target_dir, exist_ok=True)

    apex_files = []
    for ext in ['.apex', '.capex']:
        apex_files.extend(list(Path(source_dir).glob(f'*{ext}')))

    if not apex_files:
        logging.debug(f"No APEX files found in {source_dir}")
        return 0

    cleanup_stray_temp_dirs("/tmp/apex_work_*")
    cleanup_stray_temp_dirs("/tmp/apex_extract_*")

    max_workers = multiprocessing.cpu_count()

    # Process each APEX file in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        def process_apex(apex_file):
            apex_path = str(apex_file)
            return apex_path, mount_apex_direct(apex_path, target_dir)

        results = list(executor.map(process_apex, apex_files))
        success_count = sum(1 for _, success in results if success)

    logging.debug(f"Successfully processed {success_count} out of {len(apex_files)} APEX files")

    cleanup_stray_temp_dirs("/tmp/apex_work_*")

    if not keep_files:
        cleanup_post_mount(target_dir)
        cleanup_dot_files(target_dir)

    return success_count

def unmount_apexes(target_dir):
    """
    Unmount all APEXes from the target directory.

    Args:
        target_dir (str): Directory from which to unmount APEXes

    Returns:
        int: Number of successfully unmounted APEXes
    """

    count = unmount_all_apexes(target_dir)

    return count
