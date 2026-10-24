"""增值权益(entitlements)——付费点的唯一闸口。

核心功能(划词解释)永远不经过这里;只有增值功能的入口调 has() 做一次检查。
免费/收费的切换 = 改配置,不动代码:
    meter_config key `feature_<key>_free`(admin 可热改)
    > 环境变量 WENDI_FEATURE_<KEY>_FREE
    > 代码默认 free_default
值为 "0"/"false"/"no"/"off" 时视为收费(仅 Pro 可用),其余视为对所有人免费。

未来新增付费点 = FEATURES 加一项 + 对应入口挂一行 entitlements.has 检查,核心零改动。
"""
from __future__ import annotations

import os

import db

FEATURES: dict[str, dict] = {
    "sync": {"label": "云同步", "free_default": True},
    "materials": {"label": "材料解析", "free_default": True},
    "export": {"label": "导出与高级功能", "free_default": True},
}

_FALSY = ("0", "false", "no", "off")


def _config_free(feature: str) -> bool | None:
    """配置层取值;未配置返回 None(交给下一层)。"""
    raw = db.meter_get(f"feature_{feature}_free")
    if raw is None:
        raw = os.environ.get(f"WENDI_FEATURE_{feature.upper()}_FREE")
    if raw is None:
        return None
    return str(raw).strip().lower() not in _FALSY


def has(user, feature: str) -> bool:
    """该用户是否可用此增值功能。user 是已登录的 users 行(调用方先做 401)。"""
    spec = FEATURES.get(feature)
    if spec is None:
        return True                     # 未登记的功能不设闸
    free = _config_free(feature)
    if free is None:
        free = bool(spec["free_default"])
    if free:
        return True
    return db.effective_plan(user) == "pro"


def deny(handler, feature: str) -> None:
    """403 + 升级提示。handler 是本服务的 Handler(有 _json)。"""
    label = FEATURES.get(feature, {}).get("label", feature)
    handler._json(403, {
        "error": f"「{label}」是增值服务,兑换码开通后可用。",
        "entitlement": feature,
        "upgrade": True,
    })
