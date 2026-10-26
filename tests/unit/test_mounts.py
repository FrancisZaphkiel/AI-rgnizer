from __future__ import annotations

from pathlib import Path

from airgnizer.scanner.mounts import filesystem_of, mounts

MOUNTINFO = """\
22 1 0:20 /@ / rw,relatime shared:1 - btrfs /dev/nvme0n1p2 rw
30 22 0:21 /@home /home rw,relatime shared:2 - btrfs /dev/nvme0n1p2 rw
40 22 259:5 / /run/media/ana/Mi\\040Disco rw,nosuid - ntfs3 /dev/sda1 rw
41 22 8:17 / /run/media/ana/USB rw,nosuid - exfat /dev/sdb1 rw
42 22 0:50 / /mnt/nas rw - cifs //nas/descargas rw
43 22 0:51 / /mnt/vps rw - fuse.sshfs ana@vps: rw
"""


def test_mount_entries(tmp_path: Path) -> None:
    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO)
    assert [(str(m.point), m.fstype) for m in mounts(info)][2] == (
        "/run/media/ana/Mi Disco",
        "ntfs3",
    )


def test_filesystem_of_picks_longest_mount(tmp_path: Path) -> None:
    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO)

    def kind(path: str) -> tuple[str, bool]:
        entry = filesystem_of(Path(path), info)
        assert entry is not None
        return entry.fstype, entry.is_network

    assert kind("/home/ana/Descargas") == ("btrfs", False)
    assert kind("/run/media/ana/Mi Disco/Descargas") == ("ntfs3", False)
    assert kind("/run/media/ana/USB/bajadas") == ("exfat", False)
    assert kind("/mnt/nas/x") == ("cifs", True)
    assert kind("/mnt/vps/x") == ("fuse.sshfs", True)


def test_user_mounts_lists_external_drives_not_system_ones(tmp_path: Path) -> None:
    from airgnizer.scanner.mounts import user_mounts

    info = tmp_path / "mountinfo"
    info.write_text(
        "1 0 0:1 / / rw - btrfs /dev/nvme0n1p2 rw\n"
        "2 1 0:1 /@home /home rw - btrfs /dev/nvme0n1p2 rw\n"
        "3 1 0:2 / /boot/efi rw - vfat /dev/nvme0n1p1 rw\n"
        "4 1 0:3 / /run/media/ana/unidad2nvme rw - btrfs /dev/nvme1n1p1 rw\n"
        "5 1 0:4 / /run/media/ana/3A96B7FF96B7BA29 rw - ntfs3 /dev/sda1 rw\n"
        "6 1 0:5 / /mnt/fotos rw - exfat /dev/sdb1 rw\n"
        "7 1 0:6 / /srv/nas rw - nfs4 nas:/fotos rw\n"
        "8 1 0:7 / /run/media rw - tmpfs tmpfs rw\n",
        encoding="utf-8",
    )
    points = [str(e.point) for e in user_mounts(info)]
    assert points == [
        "/mnt/fotos",
        "/run/media/ana/3A96B7FF96B7BA29",
        "/run/media/ana/unidad2nvme",
        "/srv/nas",
    ]
