"""Scope for the general mechanical CAD catalog."""

import re
from pathlib import PurePosixPath


EXCLUDED_THREADS = {
    715236, 715915, 759790, 809863, 810752, 813002, 814006,
    1884198, 1892264, 1938958, 1942144, 1970334,
}
MIXED_THREADS = {716376, 723307, 761377, 776744, 803691, 1883384, 1937989}
RESTRICTED_NAME = re.compile(
    r"供弹|拨弹|八爪拨盘|弹丸|弹仓|弹链|弹舱|压弹|弹路|出弹|进弹|枪管|炮管|发射|摩擦轮|飞镖"
    r"|(?<![a-z])(?:dart|shooter|launcher|projectile|barrel|central[ _-]*loader)(?![a-z])",
    re.IGNORECASE,
)
COMPOSITE = re.compile(
    r"^(?:25hero|2025 hkust enterprize hero|hkust enterprize 2025 hero(?: 8\.7)?|uz-05-open总装|平衡总装|"
    r"(?:26联盟赛)?哨兵总装|第二代舵步|空中(?:机器人)?(?:总装)?\d*|"
    r"整机|总装|云台总装|新云台|云台p装配|p总装)$",
    re.IGNORECASE,
)


def excluded_component(thread_id, member):
    path = PurePosixPath(member.replace("\\", "/"))
    return (
        thread_id in EXCLUDED_THREADS
        or bool(RESTRICTED_NAME.search(path.name))
        or any(RESTRICTED_NAME.search(folder) for folder in path.parts[:-1])
        or (thread_id in MIXED_THREADS and bool(COMPOSITE.fullmatch(path.stem.strip())))
    )


def package_available(asset):
    thread_id = asset.get("thread_id")
    name = asset.get("name", "")
    standalone_swerve = thread_id == 761377 and name.casefold() == "2025 hkust enterprize hero swerve.rar"
    return (not asset.get("volume_paths") and not asset.get("omitted_members")
            and thread_id not in EXCLUDED_THREADS
            and (thread_id not in MIXED_THREADS or standalone_swerve))
