# Where recordings go: stick or local disk, and the room left (no real stick needed)
import os

import pytest

from app.core import config, storage

HOUR = 3600 * (100 * 48 + 1 * 1600)    # bytes per hour at the default 100 Hz IMU, 1 Hz GNSS


@pytest.fixture
def hw(tmp_path, monkeypatch):
    """A local data folder, a fake /proc/mounts and a fake /sys with one USB and one internal disk"""
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setattr(config, "LOCAL_DATA_PATH", str(local))
    monkeypatch.setattr(config, "IMU_I2C_HZ", 100)
    monkeypatch.setattr(config, "GNSS_RATE_HZ", 1)
    monkeypatch.setattr(config, "BASE_USB_PATH", str(tmp_path / "media" / "usb"))
    sysfs = tmp_path / "sys"
    (sysfs / "class" / "block").mkdir(parents=True)
    usb_dev = sysfs / "devices" / "pci0000:00" / "0000:00:14.0" / "usb2" / "2-1" / "block" / "sda" / "sda1"
    mmc_dev = sysfs / "devices" / "pci0000:00" / "0000:00:1c.0" / "mmc0" / "block" / "mmcblk2" / "mmcblk2p2"
    for dev, name in ((usb_dev, "sda1"), (mmc_dev, "mmcblk2p2")):
        dev.mkdir(parents=True)
        os.symlink(dev, sysfs / "class" / "block" / name)
    stick = tmp_path / "stick"
    stick.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/mmcblk2p2 / ext4 rw 0 0\n/dev/sda1 {stick} vfat rw 0 0\ntmpfs /run tmpfs rw 0 0\n")

    class Hw:
        pass
    h = Hw()
    h.local, h.stick, h.mounts, h.sysfs, h.tmp = local, stick, mounts, sysfs, tmp_path
    monkeypatch.setattr(storage, "MOUNTS_FILE", str(mounts))
    monkeypatch.setattr(storage, "SYSFS", str(sysfs))
    return h


def set_free(monkeypatch, per_root: dict, default=100 * HOUR):
    monkeypatch.setattr(storage, "free_bytes", lambda path: next(
        (v for root, v in per_root.items() if str(path).startswith(str(root))), default))


def test_hour_of_logging_follows_the_configured_rates(monkeypatch):
    monkeypatch.setattr(config, "IMU_I2C_HZ", 100)
    monkeypatch.setattr(config, "GNSS_RATE_HZ", 1)
    assert storage.bytes_per_hour() == HOUR == 23_040_000
    monkeypatch.setattr(config, "IMU_I2C_HZ", 400)
    assert storage.bytes_per_hour() == 3600 * (400 * 48 + 1600) == 74_880_000


def test_parse_mounts_handles_spaces_and_short_lines():
    text = "/dev/sdb1 /media/usb/MY\\040STICK vfat rw 0 0\nbroken\n\n/dev/sda1 / ext4 rw 0 0"
    assert storage.parse_mounts(text) == [("/dev/sdb1", "/media/usb/MY STICK"), ("/dev/sda1", "/")]


def test_usb_sticks_are_found_by_their_bus_not_by_where_they_are_mounted(hw):
    assert storage.usb_mounts() == [str(hw.stick)]


def test_anything_under_the_usb_base_path_counts_as_a_stick(hw):
    mount = hw.tmp / "media" / "usb" / "sdc1"
    mount.mkdir(parents=True)
    hw.mounts.write_text(hw.mounts.read_text() + f"/dev/sdc1 {mount} exfat rw 0 0\n")
    assert str(mount) in storage.usb_mounts()


def test_no_mounts_file_means_no_stick(hw, monkeypatch):
    assert storage.usb_mounts("/nonexistent/mounts") == []


def test_a_stick_is_preferred_over_the_local_disk(hw, monkeypatch):
    set_free(monkeypatch, {})
    assert storage.data_root() == os.path.join(hw.stick, storage.APP_FOLDER)
    assert os.path.isdir(os.path.join(hw.stick, storage.APP_FOLDER))
    assert storage.roots() == [os.path.join(hw.stick, storage.APP_FOLDER), str(hw.local)]


def test_without_a_stick_the_local_disk_is_used(hw, monkeypatch):
    hw.mounts.write_text("/dev/mmcblk2p2 / ext4 rw 0 0\n")
    assert storage.data_root() == str(hw.local) and storage.roots() == [str(hw.local)]


def test_a_full_stick_is_skipped(hw, monkeypatch):
    set_free(monkeypatch, {hw.stick: HOUR // 20})
    assert storage.data_root() == str(hw.local)


def test_a_read_only_stick_is_skipped(hw, monkeypatch):
    real = storage._writable
    monkeypatch.setattr(storage, "_writable", lambda p: False if str(hw.stick) in p else real(p))
    assert storage.data_root() == str(hw.local) and storage.usb_roots() == []


def test_the_stick_with_the_most_room_wins(hw, monkeypatch):
    second = hw.tmp / "usb_b"
    second.mkdir()
    hw.mounts.write_text(hw.mounts.read_text() + f"/dev/sda1 {second} vfat rw 0 0\n")
    set_free(monkeypatch, {hw.stick: 10 * HOUR, second: 50 * HOUR})
    assert storage.data_root() == os.path.join(second, storage.APP_FOLDER)


@pytest.mark.parametrize("hours, expected", [(100, "ok"), (3.0, "ok"), (2.99, "low"), (0.2, "low"),
                                             (1 / 6 - 0.001, "full"), (0, "full")])
def test_warning_levels_are_three_hours_and_ten_minutes(hw, hours, expected):
    assert storage.level(round(hours * HOUR)) == expected


def test_status_warns_below_three_hours_and_says_where(hw, monkeypatch):
    set_free(monkeypatch, {hw.stick: 2 * HOUR})
    status = storage.status()
    assert status["level"] == "low" and status["location"] == "usb" and status["hours_left"] == 2.0
    assert "2.0 h of recording" in status["message"] and "one hour takes 23.0 MB" in status["message"]


def test_status_explains_why_the_stick_is_not_used(hw, monkeypatch):
    set_free(monkeypatch, {hw.stick: 0, str(hw.local): 2 * HOUR})
    status = storage.status()
    assert status["location"] == "local" and status["usb_present"] and "USB stick is full" in status["message"]


def test_status_is_quiet_when_there_is_room(hw, monkeypatch):
    set_free(monkeypatch, {})
    status = storage.status()
    assert status["level"] == "ok" and "message" not in status


def test_projects_are_found_on_the_stick_first(hw, monkeypatch):
    set_free(monkeypatch, {})
    stick_root = storage.data_root()
    os.makedirs(os.path.join(stick_root, "both"))
    os.makedirs(os.path.join(hw.local, "both"))
    os.makedirs(os.path.join(hw.local, "only_local"))
    assert storage.find_project_path("both") == os.path.join(stick_root, "both")
    assert storage.find_project_path("only_local") == os.path.join(hw.local, "only_local")
    assert storage.find_project_path("nope") is None


@pytest.mark.parametrize("name, ok", [("walk-1", True), ("a b", True), ("é", True), ("", False), (".", False),
                                      ("..", False), ("../x", False), ("a/b", False), ("a\\b", False),
                                      (".hidden", False), (" x", False), ("x" * 101, False), ("a\0b", False)])
def test_project_names_are_one_plain_folder_name(name, ok):
    assert storage.valid_project_name(name) is ok


def test_a_local_folder_that_does_not_exist_yet_has_the_room_of_its_disk(hw, tmp_path):
    missing = tmp_path / "fresh" / "system" / "measurements"
    assert not missing.exists()
    assert storage.free_bytes(str(missing)) == storage.free_bytes(str(tmp_path)) > 0


def test_a_fresh_system_does_not_refuse_the_first_recording(hw, monkeypatch, tmp_path):
    hw.mounts.write_text("/dev/mmcblk2p2 / ext4 rw 0 0\n")
    monkeypatch.setattr(config, "LOCAL_DATA_PATH", str(tmp_path / "not" / "there" / "yet"))
    status = storage.status()
    assert status["level"] == "ok" and status["free_mb"] > 0
