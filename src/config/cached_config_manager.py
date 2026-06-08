#!/usr/bin/env python3
"""
缓存配置管理器 v2 - 支持组(group)概念
配置格式:
{
  "__version": 2,
  "__active_group": "主力",
  "__rotation": {"idle_timeout": 300},
  "__groups": {
    "主力": {
      "base_url": "https://example.com/v1",
      "auth_type": "auth_token",
      "keys": [
        {"name": "key1", "auth_token": "sk-xxx", "disabled": false},
        {"name": "key2", "auth_token": "sk-yyy", "disabled": false}
      ]
    },
    "codexFree": {
      "base_url": "https://example.com/v1",
      "auth_type": "api_key",
      "fatal_status_codes": [401, 402, 403, 429],
      "quota_status_codes": [429],
      "quota_error_types": ["usage_limit_reached"],
      "max_consecutive_fatal_failures": 3,
      "max_consecutive_quota_failures": 30,
      "max_key_retries_per_request": 50,
      "disable_quota_until_reset": true,
      "keys": [...]
    }
  }
}
"""
import json
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class CachedConfigManager:
    """带缓存的配置管理器 (v2 组格式)"""

    def __init__(self, service_name: str, cache_ttl: float = 5.0):
        self.service_name = service_name
        self.cache_ttl = cache_ttl
        self.config_dir = Path.home() / '.clp'
        self.config_file = self.config_dir / f'{service_name}.json'

        # 缓存
        self._groups_cache: Dict[str, Dict[str, Any]] = {}
        self._active_group_cache: Optional[str] = None
        self._rotation_cache: Dict[str, Any] = {'idle_timeout': 300}
        self._cache_time: float = 0
        self._file_mtime: float = 0
        self._lock = threading.RLock()

    # ── 文件 I/O ──

    def _ensure_config_dir(self):
        self.config_dir.mkdir(exist_ok=True)

    def _ensure_config_file(self) -> bool:
        self._ensure_config_dir()
        if not self.config_file.exists():
            empty_v2 = {'__version': 2, '__active_group': None, '__rotation': {'idle_timeout': 300}, '__groups': {}}
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(empty_v2, f, ensure_ascii=False, indent=2)
            return True
        return False

    def ensure_config_file(self) -> Path:
        self._ensure_config_file()
        return self.config_file

    def _read_json(self) -> Dict[str, Any]:
        self._ensure_config_file()
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError) as e:
            print(f"配置文件加载失败: {e}")
            return {}

    def _write_json(self, data: Dict[str, Any]):
        self._ensure_config_dir()
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _coerce_timestamp(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            timestamp = int(float(value))
        except (TypeError, ValueError):
            return None
        return timestamp if timestamp > 0 else None

    def _clear_expired_temporary_disabled(self, raw: Dict[str, Any]) -> bool:
        now = int(time.time())
        changed = False
        groups = raw.get('__groups', {})
        if not isinstance(groups, dict):
            return False

        for group in groups.values():
            if not isinstance(group, dict):
                continue
            keys = group.get('keys', [])
            if not isinstance(keys, list):
                continue
            for key in keys:
                if not isinstance(key, dict):
                    continue
                disabled_until = self._coerce_timestamp(key.get('disabled_until'))
                if disabled_until is None or disabled_until > now:
                    continue
                key['disabled'] = False
                key.pop('disabled_until', None)
                key.pop('disable_type', None)
                key.pop('reason', None)
                changed = True

        return changed

    # ── v1 → v2 迁移 ──

    @staticmethod
    def _is_v2(data: Dict[str, Any]) -> bool:
        return data.get('__version') == 2

    @staticmethod
    def _migrate_v1_to_v2(v1_data: Dict[str, Any]) -> Dict[str, Any]:
        """将 v1 扁平格式迁移到 v2 组格式"""
        # 按 base_url 分组
        url_buckets: Dict[str, List[Tuple[str, Dict]]] = {}
        active_v1_name: Optional[str] = None

        for name, cfg in v1_data.items():
            if not isinstance(cfg, dict) or 'base_url' not in cfg:
                continue
            url = cfg['base_url']
            url_buckets.setdefault(url, []).append((name, cfg))
            if cfg.get('active', False):
                active_v1_name = name

        # 为每个 URL 生成组名 (用域名)
        groups: Dict[str, Dict[str, Any]] = {}
        active_group: Optional[str] = None
        used_names: Dict[str, int] = {}

        for url, entries in url_buckets.items():
            # 生成组名
            parsed = urlparse(url)
            hostname = parsed.hostname or url
            if hostname in used_names:
                used_names[hostname] += 1
                group_name = f"{hostname}-{used_names[hostname]}"
            else:
                used_names[hostname] = 1
                group_name = hostname

            # 取第一条的 auth_type
            first_cfg = entries[0][1]
            auth_type = first_cfg.get('auth_type', 'auth_token')
            if not auth_type:
                auth_type = 'api_key' if first_cfg.get('api_key') else 'auth_token'

            keys: List[Dict[str, Any]] = []
            for name, cfg in entries:
                key_entry: Dict[str, Any] = {
                    'name': name,
                    'auth_token': cfg.get('auth_token', ''),
                    'disabled': False,
                }
                if cfg.get('api_key'):
                    key_entry['api_key'] = cfg['api_key']
                if cfg.get('account_id'):
                    key_entry['account_id'] = cfg['account_id']
                keys.append(key_entry)

                # 检查是否为活跃配置
                if name == active_v1_name:
                    active_group = group_name

            groups[group_name] = {
                'base_url': url,
                'auth_type': auth_type,
                'keys': keys,
            }

        if not active_group and groups:
            active_group = next(iter(groups))

        return {
            '__version': 2,
            '__active_group': active_group,
            '__rotation': {'idle_timeout': 300},
            '__groups': groups,
        }

    # ── 解析 v2 ──

    @staticmethod
    def _parse_v2(data: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Optional[str], Dict[str, Any]]:
        groups = data.get('__groups', {})
        active_group = data.get('__active_group')
        rotation = data.get('__rotation', {'idle_timeout': 300})

        # 确保 active_group 有效
        if active_group and active_group not in groups:
            active_group = None
        if not active_group and groups:
            active_group = next(iter(groups))

        return groups, active_group, rotation

    # ── 缓存 ──

    def _should_reload(self) -> bool:
        try:
            current_mtime = self.config_file.stat().st_mtime
            if current_mtime != self._file_mtime:
                return True
            if time.time() - self._cache_time > self.cache_ttl:
                return True
            return False
        except (OSError, FileNotFoundError):
            return True

    def _refresh_cache(self):
        raw = self._read_json()

        if not raw:
            self._groups_cache = {}
            self._active_group_cache = None
            self._rotation_cache = {'idle_timeout': 300}
        elif self._is_v2(raw):
            if self._clear_expired_temporary_disabled(raw):
                try:
                    self._write_json(raw)
                except OSError as e:
                    print(f"清理临时禁用状态写入失败: {e}")
            self._groups_cache, self._active_group_cache, self._rotation_cache = self._parse_v2(raw)
        else:
            # v1 → v2 迁移
            v2_data = self._migrate_v1_to_v2(raw)
            self._clear_expired_temporary_disabled(v2_data)
            try:
                self._write_json(v2_data)
            except OSError as e:
                print(f"v1→v2 迁移写入失败: {e}")
            self._groups_cache, self._active_group_cache, self._rotation_cache = self._parse_v2(v2_data)

        self._cache_time = time.time()
        try:
            self._file_mtime = self.config_file.stat().st_mtime
        except (OSError, FileNotFoundError):
            self._file_mtime = 0

    def _get_cached(self) -> Tuple[Dict[str, Dict[str, Any]], Optional[str], Dict[str, Any]]:
        with self._lock:
            if self._should_reload():
                self._refresh_cache()
            return self._groups_cache, self._active_group_cache, self._rotation_cache

    def force_reload(self):
        with self._lock:
            self._refresh_cache()

    # ── 组 API (新) ──

    @property
    def groups(self) -> Dict[str, Dict[str, Any]]:
        """所有组: {group_name: {base_url, auth_type, keys: [...]}}"""
        groups, _, _ = self._get_cached()
        # 返回深拷贝避免外部修改缓存
        return {k: {**v, 'keys': list(v.get('keys', []))} for k, v in groups.items()}

    @property
    def active_group(self) -> Optional[str]:
        """当前激活的组名"""
        _, ag, _ = self._get_cached()
        return ag

    @property
    def rotation_config(self) -> Dict[str, Any]:
        """轮转配置: {idle_timeout: 300}"""
        _, _, rot = self._get_cached()
        return dict(rot)

    def set_active_group(self, group_name: str) -> bool:
        with self._lock:
            self._refresh_cache()
            if group_name not in self._groups_cache:
                return False
            try:
                raw = self._read_json()
                if not self._is_v2(raw):
                    raw = self._migrate_v1_to_v2(raw)
                raw['__active_group'] = group_name
                self._write_json(raw)
                self._refresh_cache()
                return True
            except Exception as e:
                print(f"设置活跃组失败: {e}")
                return False

    def disable_key(
        self,
        group_name: str,
        key_name: str,
        reason: str = '',
        disabled_until: Optional[int] = None,
    ) -> bool:
        """将指定组中的 key 标记为 disabled，持久化到配置文件"""
        with self._lock:
            try:
                raw = self._read_json()
                if not self._is_v2(raw):
                    raw = self._migrate_v1_to_v2(raw)
                groups = raw.get('__groups', {})
                group = groups.get(group_name)
                if not group:
                    return False
                for key in group.get('keys', []):
                    if key.get('name') == key_name:
                        key['disabled'] = True
                        until = self._coerce_timestamp(disabled_until)
                        if until is not None:
                            key['disabled_until'] = until
                            key['disable_type'] = 'temporary'
                        else:
                            key.pop('disabled_until', None)
                            key.pop('disable_type', None)
                        if reason:
                            key['reason'] = reason
                        self._write_json(raw)
                        self._refresh_cache()
                        until_text = f" until={until}" if until is not None else ""
                        print(f"[KEY禁用] 组={group_name} key={key_name}{until_text} 原因={reason}")
                        return True
                return False
            except Exception as e:
                print(f"禁用 key 失败: {e}")
                return False

    def get_group_keys(self, group_name: str) -> List[Dict[str, Any]]:
        """获取指定组的所有 key"""
        groups, _, _ = self._get_cached()
        group = groups.get(group_name)
        if not group:
            return []
        return list(group.get('keys', []))

    def get_active_group_data(self) -> Optional[Dict[str, Any]]:
        """获取当前激活组的完整数据"""
        groups, ag, _ = self._get_cached()
        if not ag:
            return None
        group = groups.get(ag)
        if not group:
            return None
        return {**group, 'keys': list(group.get('keys', []))}

    # ── 原始数据 API (给 UI 用) ──

    def get_raw_data(self) -> Dict[str, Any]:
        """获取原始 v2 JSON, 用于 UI 读取"""
        with self._lock:
            raw = self._read_json()
            if not self._is_v2(raw):
                raw = self._migrate_v1_to_v2(raw)
            return raw

    def save_raw_data(self, data: Dict[str, Any]):
        """保存原始 v2 JSON, 用于 UI 写入"""
        with self._lock:
            self._write_json(data)
            self._refresh_cache()

    # ── 向后兼容 ──

    @property
    def configs(self) -> Dict[str, Dict[str, Any]]:
        """
        向后兼容: 返回组级字典 {group_name: {base_url, auth_type, keys, ...}}
        用于 base_proxy 中的负载均衡/路由查找
        """
        return self.groups

    @property
    def active_config(self) -> Optional[str]:
        """向后兼容: 返回活跃组名"""
        return self.active_group

    def set_active_config(self, name: str) -> bool:
        """向后兼容: 设置活跃组"""
        return self.set_active_group(name)

    def get_active_config_data(self) -> Optional[Dict[str, Any]]:
        """向后兼容"""
        return self.get_active_group_data()


# 全局实例
claude_config_manager = CachedConfigManager('claude')
codex_config_manager = CachedConfigManager('codex')
