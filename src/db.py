"""
db.py — SQLAlchemy 模型与数据库初始化。

新增领域模型：
- EchoInfo（声骸信息）
- EchoSubstat（声骸辅音）
- LoginRecord（登录记录）
"""
import datetime
import base64
import importlib
import hashlib
import os
import secrets
import socket
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event as sa_event,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import TextClause

Base = declarative_base()

TECHNICAL_NOTICE_TABLE_NAME = "你能看到这个说明你是懂技术的，不要污染数据哦"
TECHNICAL_NOTICE_MESSAGE = "这是客户端本地数据库，请不要手动修改或伪造强化数据。"

SQLCIPHER_MAIN_DB_NAME = "mc_enhance.db"
SQLCIPHER_KEY_FILE_NAME = "db_cipher_key.dpapi"
SQLCIPHER_KEY_ENV = "MC_DB_CIPHER_KEY"
SQLCIPHER_USE_ENV = "MC_DB_USE_SQLCIPHER"
SQLCIPHER_REQUIRE_ENV = "MC_DB_REQUIRE_SQLCIPHER"

class DatabaseWritePermissionError(PermissionError):
    """Raised when code attempts to write without the runtime DB write key."""


def generate_db_write_key() -> str:
    """生成本次程序运行期的数据库写入密钥。"""
    return secrets.token_urlsafe(32)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _load_sqlcipher_dbapi():
    for module_name in (
        "sqlcipher3",
        "sqlcipher3.dbapi2",
        "pysqlcipher3",
        "pysqlcipher3.dbapi2",
        "pysqlcipher",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        if hasattr(module, "connect"):
            return module
    return None


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    in_buffer = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    in_buffer = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _sqlcipher_key_file_path() -> Path:
    from src.resources import app_data_dir

    target = app_data_dir() / SQLCIPHER_KEY_FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _load_or_create_sqlcipher_key() -> str:
    env_key = os.getenv(SQLCIPHER_KEY_ENV)
    if env_key:
        return str(env_key)

    key_file = _sqlcipher_key_file_path()
    if key_file.exists():
        protected = base64.b64decode(key_file.read_text(encoding="ascii"))
        secret = _dpapi_unprotect(protected)
    else:
        secret = secrets.token_bytes(32)
        protected = _dpapi_protect(secret)
        key_file.write_text(base64.b64encode(protected).decode("ascii"), encoding="ascii")

    return base64.urlsafe_b64encode(secret).decode("ascii")


def _sql_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sqlite_database_path(path: str) -> Optional[Path]:
    try:
        url = make_url(path)
    except Exception:
        return None
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def _should_use_sqlcipher(path: str, db_file: Optional[Path]) -> bool:
    if db_file is None:
        return False
    default_enabled = db_file.name.lower() == SQLCIPHER_MAIN_DB_NAME
    return _env_flag(SQLCIPHER_USE_ENV, default_enabled)


def _can_open_sqlcipher(db_file: Path, passphrase: str, dbapi_module: Any) -> bool:
    conn = None
    try:
        conn = dbapi_module.connect(str(db_file))
        cur = conn.cursor()
        cur.execute(f"PRAGMA key = {_sql_literal(passphrase)}")
        cur.execute("SELECT count(*) FROM sqlite_master")
        cur.fetchone()
        cur.close()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _can_open_plain_sqlite(db_file: Path) -> bool:
    conn = None
    try:
        conn = sqlite3.connect(str(db_file))
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def _backup_path(db_file: Path) -> Path:
    stem = db_file.name
    base = db_file.with_name(f"{stem}.plain.bak")
    if not base.exists():
        return base
    suffix = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return db_file.with_name(f"{stem}.plain.{suffix}.bak")


def _encrypt_plain_sqlite_database(db_file: Path, passphrase: str, dbapi_module: Any):
    tmp_file = db_file.with_name(f"{db_file.name}.sqlcipher.tmp")
    backup_file = _backup_path(db_file)

    if tmp_file.exists():
        tmp_file.unlink()

    try:
        with sqlite3.connect(str(db_file)) as plain_conn:
            plain_conn.execute("PRAGMA wal_checkpoint(FULL)")
    except Exception:
        pass

    conn = dbapi_module.connect(str(db_file))
    try:
        cur = conn.cursor()
        cur.execute(
            f"ATTACH DATABASE {_sql_literal(tmp_file)} AS encrypted KEY {_sql_literal(passphrase)}"
        )
        cur.execute("SELECT sqlcipher_export('encrypted')")
        cur.execute("DETACH DATABASE encrypted")
        cur.close()
        conn.close()
    except Exception:
        try:
            conn.close()
        finally:
            if tmp_file.exists():
                tmp_file.unlink()
        raise

    os.replace(db_file, backup_file)
    os.replace(tmp_file, db_file)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_file) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    print(f"[DB] encrypted plaintext SQLite database with SQLCipher; backup={backup_file}")


def _prepare_sqlcipher_database(path: str):
    db_file = _sqlite_database_path(path)
    if not _should_use_sqlcipher(path, db_file):
        return None

    dbapi_module = _load_sqlcipher_dbapi()
    if dbapi_module is None:
        message = (
            "SQLCipher driver is not installed. Install sqlcipher3/sqlcipher3-binary "
            "or set MC_DB_USE_SQLCIPHER=0 for development."
        )
        if _env_flag(SQLCIPHER_REQUIRE_ENV, bool(getattr(sys, "frozen", False))):
            raise RuntimeError(message)
        print(f"[DB] {message} Falling back to plain SQLite.")
        return None

    passphrase = _load_or_create_sqlcipher_key()
    if db_file is not None and db_file.exists() and db_file.stat().st_size > 0:
        if not _can_open_sqlcipher(db_file, passphrase, dbapi_module):
            if _can_open_plain_sqlite(db_file):
                _encrypt_plain_sqlite_database(db_file, passphrase, dbapi_module)
            else:
                raise RuntimeError(f"Cannot open SQLCipher database with current local key: {db_file}")

    return {
        "module": dbapi_module,
        "passphrase": passphrase,
    }


class GuardedSession(SQLAlchemySession):
    """Session 默认只读；写入必须进入 write_enabled(write_key) 上下文。"""

    def __init__(self, *args, db_write_key: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._db_write_key = db_write_key
        self._db_write_depth = 0

    def _is_write_authorized(self) -> bool:
        return self._db_write_depth > 0 and bool(self._db_write_key)

    def _assert_write_authorized(self):
        if not self._is_write_authorized():
            raise DatabaseWritePermissionError("数据库默认只读；写入需要本次程序初始化生成的密钥")

    def authorize_writes(self, write_key: str):
        """用写入密钥开启当前 Session 的写权限，直到 disable_writes。"""
        if not self._db_write_key or not secrets.compare_digest(str(write_key or ""), str(self._db_write_key)):
            raise DatabaseWritePermissionError("数据库写入密钥无效")
        self._db_write_depth = max(1, self._db_write_depth)
        return self

    def disable_writes(self):
        self._db_write_depth = 0

    @contextmanager
    def write_enabled(self, write_key: str):
        if not self._db_write_key or not secrets.compare_digest(str(write_key or ""), str(self._db_write_key)):
            raise DatabaseWritePermissionError("数据库写入密钥无效")
        self._db_write_depth += 1
        try:
            yield self
        finally:
            self._db_write_depth -= 1

    def flush(self, objects=None):
        if (self.new or self.dirty or self.deleted) and not self._is_write_authorized():
            self._assert_write_authorized()
        return super().flush(objects=objects)

    def commit(self):
        if (self.new or self.dirty or self.deleted) and not self._is_write_authorized():
            self._assert_write_authorized()
        return super().commit()

    def execute(self, statement, *args, **kwargs):
        if self._statement_is_write(statement):
            self._assert_write_authorized()
        return super().execute(statement, *args, **kwargs)

    @staticmethod
    def _statement_is_write(statement) -> bool:
        if isinstance(statement, (Insert, Update, Delete)):
            return True
        if isinstance(statement, TextClause):
            text = str(statement.text or "").lstrip().lower()
            return text.startswith(
                (
                    "insert",
                    "update",
                    "delete",
                    "replace",
                    "create",
                    "alter",
                    "drop",
                    "pragma journal_mode",
                    "vacuum",
                    "reindex",
                )
            )
        return False


def local_now() -> datetime.datetime:
    """返回本机本地时间，用作数据库写入时间。"""
    return datetime.datetime.now()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 本机账号ID
    uid = Column(String(32), unique=True, nullable=False)  # 游戏UID（特征码）
    name = Column(String(64), nullable=True)  # 这台电脑的名称
    created_at = Column(DateTime, default=local_now)  # 账号创建时间（本地时间）
    account_hash = Column(String(16), unique=True, nullable=True)  # 由 id + uid + name + created_at 生成的16位hash
    total_enhance = Column(Integer, nullable=False, default=0)  # 该账号累计强化次数
    today_enhance = Column(Integer, nullable=False, default=0)  # 今日强化次数（凌晨4点刷新）
    client_enhance = Column(Integer, nullable=False, default=0)  # 当前游戏客户端强化次数（PID变化刷新）
    last_client_start_at = Column(DateTime, nullable=True)  # 游戏客户端最后一次启动时间（秒级）
    last_client_pid = Column(Integer, nullable=True)  # 游戏客户端最后一次PID
    last_sync_substat_id = Column(Integer, nullable=False, default=0)  # 已经上传echo_substats数据到第几个id


class EchoInfo(Base):
    __tablename__ = "echo_info"

    account_id = Column(Integer, ForeignKey("accounts.id"), primary_key=True)  # 归属账号ID
    echo_instance_id = Column(String(64), primary_key=True)  # 声骸实例ID
    uid = Column(String(32), nullable=False)  # 游戏UID（冗余，便于导出/分析）
    echo_name = Column(String(100), nullable=False)  # 声骸名称
    cost = Column(Integer, nullable=False)  # COST（1/3/4）
    set_name = Column(String(100), nullable=False)  # 套装名称
    main_stat = Column(String(100), nullable=False)  # 主词条
    initial_substat_count = Column(Integer, nullable=False)  # 首次生成实例ID时已有辅音数量
    created_at = Column(DateTime, default=local_now)  # 记录创建时间（本地时间）

    account = relationship("Account")
    substats = relationship("EchoSubstat", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("cost IN (1, 3, 4)", name="ck_echo_info_cost"),
        CheckConstraint("initial_substat_count BETWEEN 1 AND 5", name="ck_echo_info_initial_substat_count"),
        Index("ix_echo_info_account_created", "account_id", "created_at"),
    )

    @property
    def session_id(self) -> str:
        """兼容旧代码/测试命名：新的会话标识即 echo_instance_id。"""
        return self.echo_instance_id


# 兼容旧引用名；实际表已改为 echo_info。
EchoSession = EchoInfo


class EchoSubstat(Base):
    __tablename__ = "echo_substats"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 本地自增ID，用于上传和断点续传
    event_id = Column(String(64), nullable=False)  # 辅音记录ID（UUID）
    session_id = Column(String(64), nullable=False)  # 关联 echo_info.echo_instance_id（沿用旧列名）
    action_id = Column(String(64), nullable=True)  # 同一次开孔动作的分组ID（不再关联动作表）
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)  # 冗余账号ID
    action_type = Column(String(16), nullable=True)  # 动作类型（single/multi/unknown/history）
    action_open_count = Column(Integer, nullable=True)  # 本次动作新增孔数（1~5）
    action_start_level = Column(Integer, nullable=True)  # 动作开始时等级（5/10/15/20/25）
    action_end_level = Column(Integer, nullable=True)  # 动作结束时等级（5/10/15/20/25）
    action_span_holes = Column(String(32), nullable=True)  # 涉及孔位集合（例如"2,3"）
    slot_index = Column(Integer, nullable=True)  # 开孔序号（第1~5孔）
    level_before = Column(Integer, nullable=True)  # 开孔前等级（5/10/15/20/25）
    substat_name = Column(String(100), nullable=False)  # 副词条名称
    substat_value = Column(Float, nullable=False)  # 副词条数值
    value_tier = Column(Integer, nullable=True)  # 数值档位（1~4）
    is_historical_unknown = Column(Boolean, nullable=False, default=False)  # 是否历史未知补录
    game_day_index = Column(Integer, nullable=True)  # 游戏日序号
    is_first_enhance_of_day = Column(Boolean, nullable=True)  # 是否当天第一次强化
    is_just_logged_in = Column(Boolean, nullable=True)  # 是否刚登录
    is_just_client_restarted = Column(Boolean, nullable=True)  # 是否刚重启客户端
    restart_open_index = Column(Integer, nullable=True)  # 重启后第几次开孔
    day_enhance_count = Column(Integer, nullable=True)  # 截止当前的当日强化次数
    source_region = Column(String(64), nullable=True)  # OCR来源区域标识
    ocr_confidence = Column(Float, nullable=True)  # OCR置信度
    created_at = Column(DateTime, default=local_now)  # 记录创建时间（本地时间）

    session = relationship("EchoInfo", back_populates="substats", overlaps="account")
    account = relationship("Account", overlaps="substats,session")

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "session_id"],
            ["echo_info.account_id", "echo_info.echo_instance_id"],
            name="fk_echo_substats_echo_info",
        ),
        CheckConstraint("(action_type IS NULL OR action_type IN ('single', 'multi', 'unknown', 'history'))", name="ck_echo_substats_action_type"),
        CheckConstraint("(action_open_count IS NULL OR action_open_count BETWEEN 1 AND 5)", name="ck_echo_substats_open_count"),
        CheckConstraint(
            "(action_start_level IS NULL OR action_start_level IN (5, 10, 15, 20, 25))",
            name="ck_echo_substats_start_level",
        ),
        CheckConstraint(
            "(action_end_level IS NULL OR action_end_level IN (5, 10, 15, 20, 25))",
            name="ck_echo_substats_end_level",
        ),
        CheckConstraint("(slot_index IS NULL OR slot_index BETWEEN 1 AND 5)", name="ck_echo_substats_slot_index"),
        CheckConstraint(
            "(level_before IS NULL OR level_before IN (5, 10, 15, 20, 25))",
            name="ck_echo_substats_level_before",
        ),
        CheckConstraint("(value_tier IS NULL OR value_tier BETWEEN 1 AND 4)", name="ck_echo_substats_value_tier"),
        UniqueConstraint("event_id", name="uq_echo_substats_event_id"),
        UniqueConstraint("account_id", "session_id", "slot_index", name="uq_echo_substats_account_session_slot"),
        Index("ix_echo_substats_account_created", "account_id", "created_at"),
        Index("ix_echo_substats_game_day", "account_id", "game_day_index"),
        {"sqlite_autoincrement": True},
    )

class LoginRecord(Base):
    __tablename__ = "login_records"

    login_id = Column(String(64), primary_key=True)  # 登录记录ID（UUID）
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)  # 归属账号ID
    login_at = Column(DateTime, nullable=False)  # 登录时间（秒级）
    client_started_at = Column(DateTime, nullable=False)  # 检测到的客户端启动时间（秒级）
    client_pid = Column(Integer, nullable=True)  # 检测到的客户端进程PID
    is_client_restart = Column(Boolean, nullable=False, default=False)  # 本次是否判定为客户端重启
    created_at = Column(DateTime, default=local_now)  # 记录创建时间（本地时间）

    account = relationship("Account")

    __table_args__ = (
        Index("ix_login_records_account_login", "account_id", "login_at"),
    )


class TechnicalNotice(Base):
    __tablename__ = TECHNICAL_NOTICE_TABLE_NAME

    id = Column(Integer, primary_key=True)
    message = Column(String(255), nullable=False, default=TECHNICAL_NOTICE_MESSAGE)
    created_at = Column(DateTime, default=local_now)


_engine_cache = {}
_schema_ready_cache = set()


def make_uuid() -> str:
    return str(uuid.uuid4())


def local_machine_name() -> str:
    """返回用于账号 name 字段的本机名称。"""
    return socket.gethostname() or "unknown-host"


def hash_account_fields(
    account_id: int,
    uid: str,
    name: str,
    created_at: datetime.datetime,
) -> str:
    created_text = _normalize_to_second(created_at).isoformat() if created_at else ""
    raw = f"{int(account_id)}|{str(uid)}|{str(name)}|{created_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def hash_account_uid(uid: str) -> str:
    """兼容旧调用：仅按 UID 生成16位短 hash。新账号应使用 ensure_account_hash。"""
    return hashlib.sha256(str(uid).encode("utf-8")).hexdigest()[:16]


def ensure_account_hash(db_session, account: Account) -> str:
    """确保账号拥有由 id + uid + name + created_at 生成的16位 account_hash。"""
    if not account.name:
        account.name = local_machine_name()
    if account.created_at is None:
        account.created_at = local_now()
    if account.id is None:
        db_session.add(account)
        db_session.flush()

    account.account_hash = hash_account_fields(
        account_id=account.id,
        uid=account.uid,
        name=account.name or "",
        created_at=account.created_at,
    )
    db_session.add(account)
    db_session.flush()
    return account.account_hash


def _format_echo_substat(substat: Dict[str, Any]) -> str:
    name = str(substat.get("name") or "").strip()
    value = substat.get("value")
    is_pct = bool(substat.get("is_pct"))
    if isinstance(value, float):
        value_text = f"{value:.4f}".rstrip("0").rstrip(".")
    elif value is None:
        value_text = ""
    else:
        value_text = str(value).strip()
    return f"{name}:{value_text}:{'pct' if is_pct else 'flat'}"


def build_echo_instance_id(
    echo_name: str,
    set_name: str,
    main_stat: str,
    substats: List[Dict[str, Any]] | None,
) -> Optional[str]:
    """由声骸名、套装、主属性、已有辅音顺序生成16位实例ID。

    空白辅音声骸不生成 ID；当第一个辅音出现时才会返回稳定ID。后续新增辅音
    由 PipelineRunner 的 active context 继续沿用首次生成的 ID。
    """
    cleaned_substats = [s for s in (substats or []) if str(s.get("name") or "").strip()]
    if not cleaned_substats:
        return None

    parts = [
        str(echo_name or "").strip(),
        str(set_name or "").strip(),
        str(main_stat or "").strip(),
        *[_format_echo_substat(s) for s in cleaned_substats],
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_to_second(dt: datetime.datetime) -> datetime.datetime:
    """将时间戳归一为秒级精度。"""
    return dt.replace(microsecond=0)


@sa_event.listens_for(Account, "before_insert")
def _prepare_account_before_insert(mapper, connection, target: Account):
    if not target.name:
        target.name = local_machine_name()
    if target.created_at is None:
        target.created_at = local_now()
    if target.total_enhance is None:
        target.total_enhance = 0
    if target.today_enhance is None:
        target.today_enhance = 0
    if target.client_enhance is None:
        target.client_enhance = 0


@sa_event.listens_for(Account, "after_insert")
def _fill_account_hash_after_insert(mapper, connection, target: Account):
    target.account_hash = hash_account_fields(
        account_id=target.id,
        uid=target.uid,
        name=target.name or "",
        created_at=target.created_at,
    )
    connection.execute(
        Account.__table__.update()
        .where(Account.id == target.id)
        .values(account_hash=target.account_hash)
    )


@sa_event.listens_for(Account, "before_update")
def _refresh_account_hash_before_update(mapper, connection, target: Account):
    if target.id is None:
        return
    if not target.name:
        target.name = local_machine_name()
    if target.created_at is None:
        target.created_at = local_now()
    if target.total_enhance is None:
        target.total_enhance = 0
    if target.today_enhance is None:
        target.today_enhance = 0
    if target.client_enhance is None:
        target.client_enhance = 0
    target.account_hash = hash_account_fields(
        account_id=target.id,
        uid=target.uid,
        name=target.name or "",
        created_at=target.created_at,
    )


def mark_client_started(
    db_session,
    account: Account,
    detected_started_at: Optional[datetime.datetime] = None,
    detected_pid: Optional[int] = None,
    write_login_record: bool = True,
) -> bool:
    """
    使用最新检测到的游戏启动时间判断是否重启。

    规则：
    - 若账号无历史启动时间与PID：视为首次记录（非重启）。
    - 若新启动时间或PID不同于历史值：判定为重启。
    - 无论是否重启，都用新启动时间与PID覆盖旧值。

    返回：
    - is_restarted: 是否判定为重启。
    """
    if detected_started_at is None:
        detected_started_at = local_now()

    current_start = _normalize_to_second(detected_started_at)
    current_pid = int(detected_pid) if detected_pid is not None else None
    previous_start = account.last_client_start_at
    previous_start = _normalize_to_second(previous_start) if previous_start else None
    previous_pid = account.last_client_pid

    start_changed = previous_start is not None and current_start != previous_start
    pid_changed = previous_pid is not None and current_pid is not None and current_pid != previous_pid
    is_restarted = start_changed or pid_changed

    account.last_client_start_at = current_start
    if pid_changed:
        account.client_enhance = 0
    account.last_client_pid = current_pid
    db_session.add(account)
    db_session.flush()

    if write_login_record:
        db_session.add(
            LoginRecord(
                login_id=make_uuid(),
                account_id=account.id,
                login_at=_normalize_to_second(local_now()),
                client_started_at=current_start,
                client_pid=current_pid,
                is_client_restart=is_restarted,
            )
        )

    return is_restarted


def _migrate_add_missing_columns(engine):
    """为已有数据库补齐新增列（SQLAlchemy create_all 不会 ALTER 已存在的表）。"""
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(engine)
    migrations = [
        ("accounts", "account_hash", "VARCHAR(16)"),
        ("accounts", "total_enhance", "INTEGER NOT NULL DEFAULT 0"),
        ("accounts", "today_enhance", "INTEGER NOT NULL DEFAULT 0"),
        ("accounts", "client_enhance", "INTEGER NOT NULL DEFAULT 0"),
        ("accounts", "last_client_start_at", "DATETIME"),
        ("accounts", "last_client_pid", "INTEGER"),
        ("accounts", "created_at", "DATETIME"),
        ("accounts", "name", "VARCHAR(64)"),
        ("accounts", "last_sync_substat_id", "INTEGER NOT NULL DEFAULT 0"),
        ("login_records", "client_pid", "INTEGER"),
        ("enhance_events", "action_type", "VARCHAR(16)"),
        ("enhance_events", "action_open_count", "INTEGER"),
        ("enhance_events", "action_start_level", "INTEGER"),
        ("enhance_events", "action_end_level", "INTEGER"),
        ("enhance_events", "action_span_holes", "VARCHAR(32)"),
    ]
    with engine.begin() as conn:
        for table, column, col_type in migrations:
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                print(f"[DB] migrated: ALTER TABLE {table} ADD COLUMN {column} {col_type}")

        if inspector.has_table("accounts") and inspector.has_table("enhance_events"):
            conn.execute(
                text(
                    """
                    UPDATE accounts
                    SET total_enhance = (
                        SELECT COUNT(*)
                        FROM enhance_events
                        WHERE enhance_events.account_id = accounts.id
                          AND (enhance_events.action_type != 'unknown' OR enhance_events.action_type IS NULL)
                    )
                    WHERE COALESCE(total_enhance, 0) = 0
                    """
                )
            )

        if inspector.has_table("samples"):
            conn.execute(text("DROP TABLE IF EXISTS samples"))
            print("[DB] migrated: DROP TABLE samples")


def _migrate_echo_sessions_to_echo_info(engine):
    """把旧 echo_sessions 的静态信息迁移到 echo_info。

    旧 enhance_actions/enhance_events 通过 session_id 关联旧 session_id，因此迁移时
    将 echo_info.echo_instance_id 填为旧 session_id，以保留历史动作的 relationship。
    新数据会使用 build_echo_instance_id 生成的内容 hash。
    """
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(engine)
    if not (inspector.has_table("echo_sessions") and inspector.has_table("echo_info")):
        return

    with engine.begin() as conn:
        existing = conn.execute(text("SELECT COUNT(*) FROM echo_info")).scalar() or 0
        if existing:
            return
        conn.execute(
            text(
                """
                INSERT OR IGNORE INTO echo_info (
                    account_id,
                    echo_instance_id,
                    uid,
                    echo_name,
                    cost,
                    set_name,
                    main_stat,
                    initial_substat_count,
                    created_at
                )
                SELECT
                    es.account_id,
                    es.session_id,
                    COALESCE(a.uid, ''),
                    COALESCE(es.echo_name, ''),
                    COALESCE(es.cost, 1),
                    COALESCE(es.set_name, ''),
                    COALESCE(es.main_stat, ''),
                    COALESCE(es.initial_substat_count, 1),
                    COALESCE(es.created_at, es.started_at)
                FROM echo_sessions es
                LEFT JOIN accounts a ON a.id = es.account_id
                WHERE es.session_id IS NOT NULL
                """
            )
        )
        print("[DB] migrated: echo_sessions -> echo_info")


def _migrate_enhance_events_to_echo_substats(engine):
    """把旧 enhance_events 迁移到 echo_substats，并移除旧动作/事件表。"""
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(engine)
    if not inspector.has_table("echo_substats"):
        return

    with engine.begin() as conn:
        if inspector.has_table("enhance_actions") and inspector.has_table("enhance_events"):
            conn.execute(
                text(
                    """
                    UPDATE enhance_events
                    SET
                        action_type = COALESCE(
                            action_type,
                            (SELECT action_type FROM enhance_actions WHERE enhance_actions.action_id = enhance_events.action_id)
                        ),
                        action_open_count = COALESCE(
                            action_open_count,
                            (SELECT action_open_count FROM enhance_actions WHERE enhance_actions.action_id = enhance_events.action_id)
                        ),
                        action_start_level = COALESCE(
                            action_start_level,
                            (SELECT action_start_level FROM enhance_actions WHERE enhance_actions.action_id = enhance_events.action_id)
                        ),
                        action_end_level = COALESCE(
                            action_end_level,
                            (SELECT action_end_level FROM enhance_actions WHERE enhance_actions.action_id = enhance_events.action_id)
                        ),
                        action_span_holes = COALESCE(
                            action_span_holes,
                            (SELECT action_span_holes FROM enhance_actions WHERE enhance_actions.action_id = enhance_events.action_id)
                        )
                    WHERE action_id IS NOT NULL
                    """
                )
            )

        if inspector.has_table("enhance_events"):
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO echo_substats (
                        event_id,
                        session_id,
                        action_id,
                        account_id,
                        action_type,
                        action_open_count,
                        action_start_level,
                        action_end_level,
                        action_span_holes,
                        slot_index,
                        level_before,
                        substat_name,
                        substat_value,
                        value_tier,
                        is_historical_unknown,
                        game_day_index,
                        is_first_enhance_of_day,
                        is_just_logged_in,
                        is_just_client_restarted,
                        restart_open_index,
                        day_enhance_count,
                        source_region,
                        ocr_confidence,
                        created_at
                    )
                    SELECT
                        event_id,
                        session_id,
                        action_id,
                        account_id,
                        action_type,
                        action_open_count,
                        action_start_level,
                        action_end_level,
                        action_span_holes,
                        slot_index,
                        level_before,
                        substat_name,
                        substat_value,
                        value_tier,
                        is_historical_unknown,
                        game_day_index,
                        is_first_enhance_of_day,
                        is_just_logged_in,
                        is_just_client_restarted,
                        restart_open_index,
                        day_enhance_count,
                        source_region,
                        ocr_confidence,
                        COALESCE(created_at, opened_at)
                    FROM enhance_events
                    """
                )
            )
            conn.execute(text("DROP TABLE IF EXISTS enhance_events"))
            print("[DB] migrated: enhance_events -> echo_substats")

        if inspector.has_table("enhance_actions"):
            conn.execute(text("DROP TABLE IF EXISTS enhance_actions"))
            print("[DB] migrated: DROP TABLE enhance_actions")

        if inspector.has_table("accounts"):
            conn.execute(
                text(
                    """
                    UPDATE accounts
                    SET total_enhance = (
                        SELECT COUNT(*)
                        FROM echo_substats
                        WHERE echo_substats.account_id = accounts.id
                          AND (echo_substats.action_type != 'history' OR echo_substats.action_type IS NULL)
                    )
                    WHERE COALESCE(total_enhance, 0) = 0
                    """
                )
            )


def _migrate_echo_substats_autoincrement_id(engine):
    """把 echo_substats 从 event_id 主键迁移为本地自增 id 主键。"""
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(engine)
    if not inspector.has_table("echo_substats"):
        return

    columns = inspector.get_columns("echo_substats")
    column_names = {c["name"] for c in columns}
    pk_cols = inspector.get_pk_constraint("echo_substats").get("constrained_columns") or []
    if "id" in column_names and pk_cols == ["id"]:
        return

    old_table = "echo_substats__old_event_pk"
    copy_columns = [column.name for column in EchoSubstat.__table__.columns if column.name != "id"]
    if not {"event_id", "session_id", "account_id", "substat_name", "substat_value"}.issubset(column_names):
        return
    copy_columns_sql = ", ".join(copy_columns)
    select_parts = []
    for column_name in copy_columns:
        if column_name in column_names:
            select_parts.append(column_name)
        elif column_name == "is_historical_unknown":
            select_parts.append("0 AS is_historical_unknown")
        elif column_name == "created_at":
            select_parts.append("datetime('now', 'localtime') AS created_at")
        else:
            select_parts.append(f"NULL AS {column_name}")
    select_columns_sql = ", ".join(select_parts)
    order_sql = "created_at, rowid" if "created_at" in column_names else "rowid"
    old_indexes = [idx.get("name") for idx in inspector.get_indexes("echo_substats") if idx.get("name")]

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {old_table}"))
        conn.execute(text(f"ALTER TABLE echo_substats RENAME TO {old_table}"))
        for index_name in old_indexes:
            conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        EchoSubstat.__table__.create(conn)
        conn.execute(
            text(
                f"""
                INSERT INTO echo_substats ({copy_columns_sql})
                SELECT {select_columns_sql}
                FROM {old_table}
                ORDER BY {order_sql}
                """
            )
        )
        conn.execute(text(f"DROP TABLE IF EXISTS {old_table}"))
        print("[DB] migrated: echo_substats id INTEGER PRIMARY KEY AUTOINCREMENT")


def _drop_removed_tables(engine):
    """移除当前版本已经废弃的旧表。"""
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(engine)
    with engine.begin() as conn:
        if inspector.has_table("substat_definitions"):
            conn.execute(text("DROP TABLE IF EXISTS substat_definitions"))
            print("[DB] dropped removed table: substat_definitions")

def _ensure_technical_notice(engine):
    """在提醒表里保留一条温和提示，给直接打开库的人看。"""
    with engine.begin() as conn:
        conn.execute(
            TechnicalNotice.__table__.insert()
            .prefix_with("OR IGNORE")
            .values(id=1, message=TECHNICAL_NOTICE_MESSAGE, created_at=local_now())
        )


def init_db(path: Optional[str] = None, write_key: Optional[str] = None):
    """返回默认只读的 Session 工厂。

    传入 write_key 时会执行建表/迁移，并允许调用方通过
    ``with session.write_enabled(write_key):`` 显式开启写入。
    不传 write_key 时只建立连接与只读 Session，不会主动修改数据库结构。
    """
    if path is None:
        from src.resources import writable_db_url

        path = writable_db_url(SQLCIPHER_MAIN_DB_NAME)

    is_memory_sqlite = path in {"sqlite://", "sqlite:///:memory:"}
    cache_key = None if is_memory_sqlite else path
    if cache_key is None or cache_key not in _engine_cache:
        sqlcipher_config = None if is_memory_sqlite else _prepare_sqlcipher_database(path)
        # SQLite 的 write 并发在多进程下非常容易出现 database is locked。
        # 我们这里增加 pool_timeout 并通过 connect_args 设置更大的 timeout
        engine_kwargs = {
            "echo": False,
            "connect_args": {"timeout": 60.0, "check_same_thread": False},  # SQLite busy timeout (seconds)
        }
        if sqlcipher_config:
            engine_kwargs["module"] = sqlcipher_config["module"]
        if is_memory_sqlite:
            engine_kwargs["poolclass"] = StaticPool
        else:
            engine_kwargs.update(
                pool_recycle=3600,
                pool_size=10,
                max_overflow=20,
            )
        engine = create_engine(path, **engine_kwargs)

        # 为每个新连接启用 WAL 模式和 busy_timeout（支持多进程并发读写）
        @sa_event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            if sqlcipher_config:
                cursor.execute(f"PRAGMA key = {_sql_literal(sqlcipher_config['passphrase'])}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA temp_store=memory")
            cursor.execute("PRAGMA mmap_size=30000000")
            cursor.close()

        if cache_key is None:
            if write_key:
                _migrate_add_missing_columns(engine)
                Base.metadata.create_all(engine)
                _migrate_echo_sessions_to_echo_info(engine)
                _migrate_echo_substats_autoincrement_id(engine)
                _migrate_enhance_events_to_echo_substats(engine)
                _migrate_echo_substats_autoincrement_id(engine)
                _drop_removed_tables(engine)
                _ensure_technical_notice(engine)
            return sessionmaker(bind=engine, class_=GuardedSession, db_write_key=write_key)
        _engine_cache[cache_key] = engine

    engine = _engine_cache[cache_key]
    schema_key = cache_key
    if write_key and schema_key not in _schema_ready_cache:
        _migrate_add_missing_columns(engine)
        Base.metadata.create_all(engine)
        _migrate_echo_sessions_to_echo_info(engine)
        _migrate_echo_substats_autoincrement_id(engine)
        _migrate_enhance_events_to_echo_substats(engine)
        _migrate_echo_substats_autoincrement_id(engine)
        _drop_removed_tables(engine)
        _ensure_technical_notice(engine)
        _schema_ready_cache.add(schema_key)

    return sessionmaker(bind=engine, class_=GuardedSession, db_write_key=write_key)


if __name__ == "__main__":
    write_key = generate_db_write_key()
    Session = init_db(write_key=write_key)
    s = Session()
    print("db ok")
    s.close()
