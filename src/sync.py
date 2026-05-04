"""
sync.py — 本地数据到云端的大数据同步模块。
负责将本地 SQLite 的强化记录定期发送到中心服务器，使用 AES-GCM 保证数据传输的抗篡改与机密性。
"""
import threading
import time
import json
import os
import re
import requests
import base64
from typing import Any, Dict, Optional
from src.db import (
    Account,
    EchoInfo,
    EchoSubstat,
    generate_db_write_key,
    init_db,
)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key
except ImportError:
    AESGCM = None
    ec = None

# 没有更新吗？
class DataSyncWorker(threading.Thread):
    def __init__(
        self,
        db_path: Optional[str] = None,
        interval_sec=30,
        db_write_key: Optional[str] = None,
    ):
        super().__init__(daemon=True, name="DataSyncWorker")
        self.db_path = db_path
        self.db_write_key = db_write_key or generate_db_write_key()
        self.Session = init_db(db_path, write_key=self.db_write_key)
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._stats_lock = threading.Lock()
        self._server_total_count: Optional[int] = None
        self._server_today_count: Optional[int] = None
        self._last_server_message: str = ""
        self._last_server_update_at: Optional[float] = None
        self._public_ip: Optional[str] = None
        self._public_ip_raw: str = ""
        self._public_ip_fetched_at: Optional[float] = None
        self.ip_lookup_urls = ("https://myip.ipip.net", "http://myip.ipip.net")
        self.api_url = "https://mc.ffxiv.ws/api/upload_substats"
        
        # ECC 公钥（基于 SECP256R1/P-256 曲线）。公钥直接硬编码即可，它只负责“上锁”，发给所有客户端也是绝对安全的。
        # 服务端必须保管好配套的 private key 才能解开用该公钥协商出的加密数据包裹。
        self.server_pub_key_pem = b"""-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE+poVQUE+VD8SuysR3B91DmIHHkJn
AsbANpWRrfbOPdlJWm+WE4jq7uozLpkJJdArVM/Go2NopB6Vp1vV6bo+Lg==
-----END PUBLIC KEY-----"""
        
        if AESGCM is None or ec is None:
            print("[SyncWorker] 警告: 未安装 cryptography 库，无法行使加密传输。")
            
    def stop(self):
        self._stop_event.set()

    def get_server_counts(self) -> Dict[str, Any]:
        """返回最近一次云端确认的全局样本统计。"""
        with self._stats_lock:
            return {
                "total_count": self._server_total_count,
                "today_count": self._server_today_count,
                "message": self._last_server_message,
                "updated_at": self._last_server_update_at,
            }

    @staticmethod
    def _parse_count(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_sync_time(value: Any) -> Optional[str]:
        if value is None:
            return None
        if hasattr(value, "replace") and hasattr(value, "strftime"):
            return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        return str(value).split(".", 1)[0]

    def _update_server_counts(self, resp_json: Dict[str, Any]):
        total_count = self._parse_count(resp_json.get("total_count"))
        today_count = self._parse_count(resp_json.get("today_count"))
        message = str(resp_json.get("message") or "")

        with self._stats_lock:
            if total_count is not None:
                self._server_total_count = total_count
            if today_count is not None:
                self._server_today_count = today_count
            self._last_server_message = message
            if total_count is not None or today_count is not None:
                self._last_server_update_at = time.time()

    def _encrypt_data(self, json_str: str) -> str:
        """基于 ECC (Elliptic Curve Cryptography) + AES-GCM 的混合加密方案 (类似 ECIES)"""
        # 1. 载入服务端的 ECC 公钥 (SECP256R1 / P-256)
        server_public_key = load_pem_public_key(self.server_pub_key_pem)
        
        # 2. 客户端为本次请求随机生成【临时密钥对 (Ephemeral Key)】
        ephemeral_private_key = ec.generate_private_key(ec.SECP256R1())
        ephemeral_public_key_bytes = ephemeral_private_key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        ) # 固定为 65 字节的公钥序列
        
        # 3. ECDH 密钥交换：临时私钥 + 服务端公钥 计算出 -> 共享密钥 (Shared Secret)
        shared_secret = ephemeral_private_key.exchange(ec.ECDH(), server_public_key)
        
        # 4. HKDF 密钥派生：将协商的曲线点共享密钥规范化为强随机的 32 字节 AES 密钥
        derived_aes_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b'mc_sync_v1',
        ).derive(shared_secret)
        
        # 5. 使用派生的 AES 密钥将原始 JSON 内容进行加密
        aesgcm = AESGCM(derived_aes_key)
        nonce = os.urandom(12)
        cipher_text = aesgcm.encrypt(nonce, json_str.encode('utf-8'), associated_data=None)
        
        # 6. 将各参数按结构打包发往后端：
        # [临时公钥 65 bytes] + [GCM 随机 Nonce 12 bytes] + [密文与Tag验证体]
        payload = ephemeral_public_key_bytes + nonce + cipher_text
        return base64.b64encode(payload).decode('utf-8')

    @staticmethod
    def _parse_public_ip(text: str) -> Optional[str]:
        """从 myip.ipip.net 返回文案中提取 IPv4/IPv6 地址。"""
        if not text:
            return None

        ipv4_match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        if ipv4_match:
            return ipv4_match.group(0)

        ipv6_match = re.search(r"\b[0-9a-fA-F:]{2,}:[0-9a-fA-F:]{2,}\b", text)
        if ipv6_match:
            return ipv6_match.group(0)

        return None

    def _get_public_ip_info(self) -> Dict[str, Any]:
        """访问 myip.ipip.net 获取客户端公网 IP，失败时不阻断上传。"""
        now = time.time()
        if self._public_ip and self._public_ip_fetched_at and now - self._public_ip_fetched_at < 3600:
            return {
                "client_ip": self._public_ip,
                "client_ip_source": "myip.ipip.net",
                "client_ip_raw": self._public_ip_raw,
                "client_ip_fetched_at": self._public_ip_fetched_at,
            }

        last_error = None
        for url in self.ip_lookup_urls:
            try:
                resp = requests.get(url, timeout=3)
                resp.raise_for_status()
                raw_text = resp.text.strip()
                public_ip = self._parse_public_ip(raw_text)
                if public_ip:
                    self._public_ip = public_ip
                    self._public_ip_raw = raw_text[:300]
                    self._public_ip_fetched_at = now
                    return {
                        "client_ip": self._public_ip,
                        "client_ip_source": "myip.ipip.net",
                        "client_ip_raw": self._public_ip_raw,
                        "client_ip_fetched_at": self._public_ip_fetched_at,
                    }
                last_error = f"未解析到IP: {raw_text[:120]}"
            except Exception as e:
                last_error = e

        print(f"[SyncWorker] 获取公网IP失败，将继续上传样本: {last_error}")

        return {
            "client_ip": self._public_ip,
            "client_ip_source": "myip.ipip.net",
            "client_ip_raw": self._public_ip_raw,
            "client_ip_fetched_at": self._public_ip_fetched_at,
        }

    def run(self):
        if AESGCM is None or ec is None:
            return
            
        print(f"[SyncWorker] 本地大数据云端同步后台已启动。目标节点: {self.api_url}")
        
        # 让游戏和录制先跑起来，延迟一会再做第一次同步
        self._stop_event.wait(10)
        
        while not self._stop_event.is_set():
            try:
                with self.Session() as sess:
                    # 每次最多批量上传 100 条未同步的干净记录，依据各自所属 Account 的 last_sync_substat_id 判断
                    records = (sess.query(EchoSubstat)
                               .join(Account, EchoSubstat.account_id == Account.id)
                               .filter(EchoSubstat.id > Account.last_sync_substat_id)
                               .order_by(EchoSubstat.id.asc())
                               .limit(100).all())
                    
                    account_map = {}
                    echo_info_map = {}
                    echo_substats_list = []
                    max_id_per_account = {}

                    if records:
                        for r in records:
                            # 3. Add account
                            if r.account_id not in account_map:
                                acc = r.account
                                if acc:
                                    account_map[r.account_id] = {
                                        "uid": acc.uid,
                                        "name": acc.name,
                                        "created_at": self._format_sync_time(acc.created_at),
                                        "account_hash": acc.account_hash,
                                        "total_enhance": acc.total_enhance,
                                        "today_enhance": acc.today_enhance,
                                        "client_enhance": acc.client_enhance,
                                    }

                            # 2. Add echo info
                            if r.session_id not in echo_info_map:
                                echo_info = sess.query(EchoInfo).filter_by(echo_instance_id=r.session_id).first()
                                if echo_info:
                                    echo_info_map[r.session_id] = {
                                        "echo_instance_id": echo_info.echo_instance_id,
                                        "uid": echo_info.uid,
                                        "echo_name": echo_info.echo_name,
                                        "cost": echo_info.cost,
                                        "set_name": echo_info.set_name,
                                        "main_stat": echo_info.main_stat,
                                        "initial_substat_count": echo_info.initial_substat_count,
                                        "created_at": self._format_sync_time(echo_info.created_at),
                                    }

                            # 1. Add substat
                            acc_uid = r.account.uid if r.account else None
                            substat_item = {
                                "uid": acc_uid,
                                "id": r.id,
                                "event_id": r.event_id,
                                "session_id": r.session_id,
                                "action_id": r.action_id,
                                "account_id": r.account_id,
                                "action_type": r.action_type,
                                "action_open_count": r.action_open_count,
                                "action_start_level": r.action_start_level,
                                "action_end_level": r.action_end_level,
                                "action_span_holes": r.action_span_holes,
                                "slot_index": r.slot_index,
                                "level_before": r.level_before,
                                "substat_name": r.substat_name,
                                "substat_value": r.substat_value,
                                "value_tier": r.value_tier,
                                "is_historical_unknown": r.is_historical_unknown,
                                "game_day_index": r.game_day_index,
                                "is_first_enhance_of_day": r.is_first_enhance_of_day,
                                "is_just_logged_in": r.is_just_logged_in,
                                "is_just_client_restarted": r.is_just_client_restarted,
                                "restart_open_index": r.restart_open_index,
                                "day_enhance_count": r.day_enhance_count,
                                "source_region": r.source_region,
                                "ocr_confidence": r.ocr_confidence,
                                "created_at": self._format_sync_time(r.created_at),
                            }
                            echo_substats_list.append(substat_item)
                            
                            max_id_per_account[r.account_id] = max(max_id_per_account.get(r.account_id, 0), r.id)
                        
                    client_ip_info = self._get_public_ip_info()
                    payload_data = {
                        **client_ip_info,
                        "last_active_at": int(time.time()),
                        "accounts": list(account_map.values()),
                        "echo_info": list(echo_info_map.values()),
                        "echo_substats": echo_substats_list
                    }
                    
                    json_str = json.dumps(payload_data, ensure_ascii=False)
                    encrypted_payload = {
                        "client_version": "1.0",
                        "data": self._encrypt_data(json_str)
                    }
                    
                    # 无论是否有新的强化样本都发送心跳保证活跃状态
                    resp = requests.post(self.api_url, json=encrypted_payload, timeout=10)
                    if resp.status_code == 200:
                        try:
                            resp_json = resp.json()
                            if resp_json.get("status") == "success":
                                self._update_server_counts(resp_json)
                                counts = self.get_server_counts()
                                if echo_substats_list:
                                    print(
                                        f"[SyncWorker] 成功打包上传了 {len(echo_substats_list)} 条强化样本至远端大数据中心! "
                                        f"服务器响应已确认。total_count={counts.get('total_count')} "
                                        f"today_count={counts.get('today_count')}"
                                    )
                                # 更新各个 Account 的 last_sync_substat_id
                                with sess.write_enabled(self.db_write_key):
                                    for acc_id, m_id in max_id_per_account.items():
                                        sess.query(Account).filter_by(id=acc_id).update({"last_sync_substat_id": m_id})
                                    sess.commit()
                            else:
                                print(f"[SyncWorker] 上传未能确认，服务器返回非success状态: {resp_json}")
                        except Exception as parse_e:
                            print(f"[SyncWorker] 服务器返回了200但非期望的JSON响应格式: {parse_e}")
                    else:
                        print(f"[SyncWorker] 上传失败，服务器拒绝请求，状态码: {resp.status_code}")
                            
            except Exception as e:
                print(f"[SyncWorker] 同步循环遇到网络或解析异常: {e}")
                
            # 每隔指定周期（例如 5分钟=300秒）轮询一次新数据
            self._stop_event.wait(self.interval_sec)
