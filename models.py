from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

task_accounts = db.Table(
    'task_accounts',
    db.Column('task_id', db.Integer, db.ForeignKey('scheduled_tasks.id'), primary_key=True),
    db.Column('account_id', db.Integer, db.ForeignKey('accounts.id'), primary_key=True),
)


class Account(db.Model):
    """Telegram账号"""
    __tablename__ = 'accounts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    api_id = db.Column(db.Integer, nullable=False)
    api_hash = db.Column(db.String(100), nullable=False)
    session_string = db.Column(db.Text)
    session_kind = db.Column(db.String(20), default='telethon')
    is_active = db.Column(db.Boolean, default=True)
    # pending / authorized / error
    status = db.Column(db.String(50), default='pending')
    # 托管时间限制：False=全天，True=仅 start~end 时段自动执行
    schedule_enabled = db.Column(db.Boolean, default=False)
    schedule_start = db.Column(db.String(5), default='18:00')   # HH:MM
    schedule_end = db.Column(db.String(5), default='08:00')     # HH:MM
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    keywords = db.relationship('Keyword', backref='account', lazy=True,
                               foreign_keys='Keyword.account_id')
    scheduled_tasks = db.relationship('ScheduledTask', backref='account', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'phone': self.phone,
            'api_id': self.api_id,
            'is_active': self.is_active,
            'status': self.status,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        }


class Keyword(db.Model):
    """关键词规则：检测回复中的关键词并自动回复"""
    __tablename__ = 'keywords'

    id = db.Column(db.Integer, primary_key=True)
    # NULL 表示对所有账号生效
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True)
    keyword = db.Column(db.String(500), nullable=False)
    # 是否需要从消息中解析等待时间
    has_time_requirement = db.Column(db.Boolean, default=False)
    # 在解析出的时间基础上额外等待的秒数（固定缓冲）
    time_buffer_seconds = db.Column(db.Integer, default=30)
    # 随机缓冲：若 buffer_random_max > 0，则缓冲时间从 [buffer_random_min, buffer_random_max] 随机取
    buffer_random_min = db.Column(db.Integer, default=0)
    buffer_random_max = db.Column(db.Integer, default=0)
    reply_message = db.Column(db.Text, nullable=False)
    image = db.Column(db.String(100), nullable=True)
    # 指定发送目标群组（可选）。填写后回复固定发到此群组，而非触发消息所在的聊天
    target_group_id = db.Column(db.String(100))
    target_group_name = db.Column(db.String(200), default='')
    # 触发模式：reply_to_me=引用回复, mention_me=@提及, all_messages=所有消息
    trigger_mode = db.Column(db.String(20), default='reply_to_me')
    # 指定发送到群组内的哪个话题（Forum Topic ID），None 表示不指定（发到默认/通用话题）
    topic_id = db.Column(db.Integer, nullable=True)
    # 随机等待时间：开启后忽略解析时间，在 [min, max] 秒内随机选一个等待时间
    use_random_time = db.Column(db.Boolean, default=False)
    random_min_seconds = db.Column(db.Integer, default=60)
    random_max_seconds = db.Column(db.Integer, default=300)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'keyword': self.keyword,
            'has_time_requirement': self.has_time_requirement,
            'time_buffer_seconds': self.time_buffer_seconds,
            'buffer_random_min': self.buffer_random_min,
            'buffer_random_max': self.buffer_random_max,
            'reply_message': self.reply_message,
            'trigger_mode': self.trigger_mode or 'reply_to_me',
            'target_group_id': self.target_group_id,
            'target_group_name': self.target_group_name,
            'topic_id': self.topic_id,
            'use_random_time': self.use_random_time,
            'random_min_seconds': self.random_min_seconds,
            'random_max_seconds': self.random_max_seconds,
            'is_active': self.is_active,
        }


class ScheduledTask(db.Model):
    """定时发送消息任务"""
    __tablename__ = 'scheduled_tasks'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    group_id = db.Column(db.String(100), nullable=False)
    group_name = db.Column(db.String(200), default='')
    topic_id = db.Column(db.Integer, nullable=True)   # Forum 话题 ID
    message = db.Column(db.Text, nullable=False)
    image = db.Column(db.String(100), nullable=True)
    # interval: 按间隔；cron: 按cron表达式
    task_type = db.Column(db.String(20), default='interval')
    interval_minutes = db.Column(db.Integer)
    cron_expression = db.Column(db.String(100))
    once_at = db.Column(db.DateTime)
    send_immediately = db.Column(db.Boolean, default=False)
    account_mode = db.Column(db.String(20), default='single', nullable=False)
    last_account_id = db.Column(db.Integer)
    participants = db.relationship('Account', secondary=task_accounts, order_by='Account.id')
    revision = db.Column(db.Integer, default=0, nullable=False)
    delivery_state = db.Column(db.String(20), default='ready', nullable=False)
    completed_at = db.Column(db.DateTime)
    last_error = db.Column(db.Text, default='')
    caption_mode = db.Column(db.String(20), default='fixed', nullable=False)
    caption_language = db.Column(db.String(80), default='ru', nullable=False)
    caption_max_chars = db.Column(db.Integer, default=300, nullable=False)
    caption_use_image = db.Column(db.Boolean, default=False, nullable=False)
    caption_instructions = db.Column(db.Text, default='', nullable=False)
    images = db.relationship('TaskImage', cascade='all, delete-orphan', order_by='TaskImage.id', lazy='select')
    # 随机延迟：在触发时间后额外随机等待 [min, max] 秒，0 表示不启用
    random_delay_min = db.Column(db.Integer, default=0)
    random_delay_max = db.Column(db.Integer, default=0)
    # 上次实际执行时间（记录于 _send_scheduled_message 执行成功后，用于断点续时）
    last_run_at = db.Column(db.DateTime, nullable=True)
    # 上次调度器记录的下次运行时间（辅助字段，保留备用）
    next_run_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def sending_accounts(self):
        return self.participants or ([self.account] if self.account else [])

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'account_ids': [account.id for account in self.sending_accounts],
            'group_id': self.group_id,
            'group_name': self.group_name,
            'message': self.message,
            'task_type': self.task_type,
            'interval_minutes': self.interval_minutes,
            'cron_expression': self.cron_expression,
            'random_delay_min': self.random_delay_min,
            'random_delay_max': self.random_delay_max,
            'is_active': self.is_active,
        }


class TaskImage(db.Model):
    __tablename__ = 'task_images'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('scheduled_tasks.id'), nullable=False, index=True)
    filename = db.Column(db.String(100), nullable=False)
    digest = db.Column(db.String(64), nullable=False)
    state = db.Column(db.String(20), default='pending', nullable=False)
    sent_at = db.Column(db.DateTime)
    caption = db.Column(db.Text)
    sent_by_account_id = db.Column(db.Integer)
    __table_args__ = (db.UniqueConstraint('task_id', 'digest'),)


class PendingReply(db.Model):
    """待发送的定时回复（由关键词+时间规则触发）"""
    __tablename__ = 'pending_replies'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    group_id = db.Column(db.String(100), nullable=False)
    keyword_id = db.Column(db.Integer, db.ForeignKey('keywords.id'), nullable=True)
    topic_id = db.Column(db.Integer, nullable=True)   # Forum 话题 ID，None 表示不指定
    message = db.Column(db.Text, nullable=False)
    image = db.Column(db.String(100), nullable=True)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    is_sent = db.Column(db.Boolean, default=False)
    sent_at = db.Column(db.DateTime)
    triggered_by = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MessageLog(db.Model):
    """操作日志"""
    __tablename__ = 'message_logs'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True)
    group_id = db.Column(db.String(100), default='')
    group_name = db.Column(db.String(200), default='')
    # sent / keyword_matched / auto_replied / scheduled_sent / error
    log_type = db.Column(db.String(50))
    content = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Whitelist(db.Model):
    """白名单：只有列表中的实体（用户/频道/机器人）引用我的消息时才响应"""
    __tablename__ = 'whitelist'

    id = db.Column(db.Integer, primary_key=True)
    # NULL 表示对所有账号生效
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True)
    # Telegram 实体 ID（正数为用户/机器人，负数为频道）
    entity_id = db.Column(db.String(50), nullable=False)
    entity_name = db.Column(db.String(200), default='')
    # user / bot / channel
    entity_type = db.Column(db.String(20), default='user')
    note = db.Column(db.String(200), default='')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'entity_id': self.entity_id,
            'entity_name': self.entity_name,
            'entity_type': self.entity_type,
            'note': self.note,
            'is_active': self.is_active,
        }


class TargetEntity(db.Model):
    """目标列表：保存曾使用过的发送目标（群组/频道/用户/机器人）"""
    __tablename__ = 'target_entities'

    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.String(50), unique=True, nullable=False)  # Telegram 数字 ID
    name = db.Column(db.String(200), default='')   # 显示名称
    # user / bot / group / supergroup / channel / unknown
    entity_type = db.Column(db.String(20), default='unknown')
    note = db.Column(db.String(200), default='')
    topic_id = db.Column(db.Integer, nullable=True)   # 关联的 Forum 话题 ID（可选）
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'entity_id': self.entity_id,
            'name': self.name,
            'entity_type': self.entity_type,
            'note': self.note,
            'topic_id': self.topic_id,
        }


class AIAgent(db.Model):
    __tablename__ = 'ai_agents'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True)
    account = db.relationship('Account', backref='ai_agents')
    name = db.Column(db.String(100), nullable=False)
    chat_id = db.Column(db.String(100), unique=True, nullable=False)
    channel_id = db.Column(db.String(100), default='')
    mode = db.Column(db.String(20), default='OFF', nullable=False)
    consent = db.Column(db.Boolean, default=False, nullable=False)
    semantic_memory = db.Column(db.Boolean, default=False, nullable=False)
    memory_error = db.Column(db.Text, default='', nullable=False)
    memory_retry_at = db.Column(db.DateTime)
    community_replies = db.Column(db.Boolean, default=False, nullable=False)
    support_router = db.Column(db.Boolean, default=True, nullable=False)
    information = db.Column(db.Boolean, default=True, nullable=False)
    vision = db.Column(db.Boolean, default=False, nullable=False)
    fallback_language = db.Column(db.String(30), default='AMHARIC', nullable=False)
    language_profile = db.Column(db.String(20), default='ethiopia', nullable=False)
    custom_language = db.Column(db.String(80), default='', nullable=False)
    language_instructions = db.Column(db.Text, default='', nullable=False)
    glossary = db.Column(db.Text, default='', nullable=False)
    support_contact = db.Column(db.String(200), default='support@betjam.com', nullable=False)
    support_text = db.Column(db.Text, default='', nullable=False)
    privacy_text = db.Column(db.Text, default='', nullable=False)
    scam_detection = db.Column(db.Boolean, default=True, nullable=False)
    allow_delete = db.Column(db.Boolean, default=False, nullable=False)
    allow_ban = db.Column(db.Boolean, default=False, nullable=False)
    ban_confidence = db.Column(db.Float, default=0.98, nullable=False)
    delete_personal_data = db.Column(db.Boolean, default=True, nullable=False)
    delete_payment_data = db.Column(db.Boolean, default=True, nullable=False)
    delete_identity_documents = db.Column(db.Boolean, default=True, nullable=False)
    reply_confidence = db.Column(db.Float, default=0.70, nullable=False)
    moderation_confidence = db.Column(db.Float, default=0.90, nullable=False)
    vision_confidence = db.Column(db.Float, default=0.93, nullable=False)
    chat_cooldown = db.Column(db.Integer, default=45, nullable=False)
    user_cooldown = db.Column(db.Integer, default=120, nullable=False)
    intent_cooldown = db.Column(db.Integer, default=300, nullable=False)
    fact_max_age_hours = db.Column(db.Integer, default=72, nullable=False)
    revision = db.Column(db.Integer, default=0, nullable=False)
    knowledge_revision = db.Column(db.Integer, default=0, nullable=False)
    last_analysis_at = db.Column(db.DateTime)
    last_reply_at = db.Column(db.DateTime)
    last_error = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AIMessage(db.Model):
    __tablename__ = 'ai_messages'
    __table_args__ = (db.UniqueConstraint('agent_id', 'message_id'),)
    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey('ai_agents.id'), nullable=False, index=True)
    message_id = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.String(50), default='')
    text = db.Column(db.Text, default='')
    fingerprint = db.Column(db.String(64), nullable=False)
    is_ai = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AIUserContext(db.Model):
    __tablename__ = 'ai_user_context'
    __table_args__ = (db.UniqueConstraint('agent_id', 'user_id'),)
    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey('ai_agents.id'), nullable=False)
    user_id = db.Column(db.String(50), nullable=False)
    language = db.Column(db.String(30), default='UNKNOWN')
    greeted = db.Column(db.Boolean, default=False)
    last_reply_at = db.Column(db.DateTime)
    last_intent = db.Column(db.String(40), default='UNKNOWN')


class AIFact(db.Model):
    __tablename__ = 'ai_facts'
    __table_args__ = (db.UniqueConstraint('agent_id', 'source_key'),)
    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey('ai_agents.id'), nullable=False, index=True)
    source_key = db.Column(db.String(160), nullable=False)
    source_url = db.Column(db.String(500), nullable=False)
    source_message_id = db.Column(db.Integer)
    related_id = db.Column(db.Integer, db.ForeignKey('ai_facts.id'))
    title = db.Column(db.String(200), default='')
    summary = db.Column(db.Text, default='')
    code = db.Column(db.String(100), default='')
    status = db.Column(db.String(20), default='UNKNOWN')
    end_at = db.Column(db.DateTime)
    approved = db.Column(db.Boolean, default=False)
    deleted = db.Column(db.Boolean, default=False)
    fingerprint = db.Column(db.String(64), default='')
    revision = db.Column(db.Integer, default=0, nullable=False)
    verified_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    history = db.relationship('AIFactHistory', backref='fact', cascade='all, delete-orphan')


class AIFactHistory(db.Model):
    __tablename__ = 'ai_fact_history'
    id = db.Column(db.Integer, primary_key=True)
    fact_id = db.Column(db.Integer, db.ForeignKey('ai_facts.id'), nullable=False)
    snapshot = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AIDecision(db.Model):
    __tablename__ = 'ai_decisions'
    __table_args__ = (db.UniqueConstraint('agent_id', 'message_id', 'fingerprint'),)
    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey('ai_agents.id'), nullable=False, index=True)
    agent = db.relationship('AIAgent')
    message_id = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.String(50), default='')
    fingerprint = db.Column(db.String(64), nullable=False)
    agent_revision = db.Column(db.Integer, nullable=False)
    knowledge_revision = db.Column(db.Integer, nullable=False)
    language = db.Column(db.String(30), default='UNKNOWN')
    intent = db.Column(db.String(40), default='UNKNOWN')
    confidence = db.Column(db.Float, default=0)
    action = db.Column(db.String(20), default='HUMAN_REVIEW')
    classification = db.Column(db.String(30), default='UNKNOWN')
    reply = db.Column(db.Text, default='')
    reason = db.Column(db.Text, default='')
    fact_ids = db.Column(db.JSON, default=list)
    rule_ids = db.Column(db.JSON, default=list)
    training_needed = db.Column(db.Boolean, default=False, nullable=False)
    training_status = db.Column(db.String(20), default='OPEN', nullable=False)
    has_image = db.Column(db.Boolean, default=False)
    has_reply = db.Column(db.Boolean, default=False, nullable=False)
    embedding = db.Column(db.JSON)
    embedding_hash = db.Column(db.String(64), default='')
    memory_method = db.Column(db.String(20), default='words', nullable=False)
    memory_example_ids = db.Column(db.JSON, default=list, nullable=False)
    ban_status = db.Column(db.String(20), default='', nullable=False)
    delete_status = db.Column(db.String(20), default='', nullable=False)
    state = db.Column(db.String(20), default='ANALYZING', nullable=False, index=True)
    result = db.Column(db.Text, default='')
    model = db.Column(db.String(100), default='')
    analysis_attempts = db.Column(db.Integer, default=0, nullable=False)
    next_analysis_at = db.Column(db.DateTime, index=True)
    analysis_started_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completed_at = db.Column(db.DateTime)


class AIExample(db.Model):
    __tablename__ = 'ai_examples'
    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey('ai_agents.id'), nullable=False, index=True)
    title = db.Column(db.String(100), nullable=False)
    text = db.Column(db.Text, default='', nullable=False)
    reply_context = db.Column(db.Text, default='', nullable=False)
    image = db.Column(db.LargeBinary)
    expected = db.Column(db.JSON, default=dict, nullable=False)
    corrected_reply = db.Column(db.Text, default='', nullable=False)
    guidance = db.Column(db.Text, default='', nullable=False)
    embedding = db.Column(db.JSON)
    embedding_hash = db.Column(db.String(64), default='')
    kind = db.Column(db.String(20), default='test', nullable=False)
    approved = db.Column(db.Boolean, default=False, nullable=False)
    source_decision_id = db.Column(db.Integer)
    running = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    runs = db.relationship('AIReplayRun', cascade='all, delete-orphan', order_by='AIReplayRun.id.desc()')


class AIModerationRule(db.Model):
    __tablename__ = 'ai_moderation_rules'
    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey('ai_agents.id'), nullable=False, index=True)
    title = db.Column(db.String(100), nullable=False)
    example = db.Column(db.Text, nullable=False)
    guidance = db.Column(db.Text, nullable=False)
    action = db.Column(db.String(20), nullable=False)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AIReplayRun(db.Model):
    __tablename__ = 'ai_replay_runs'
    __table_args__ = (db.UniqueConstraint('example_id', 'request_token'),)
    id = db.Column(db.Integer, primary_key=True)
    example_id = db.Column(db.Integer, db.ForeignKey('ai_examples.id'), nullable=False, index=True)
    request_token = db.Column(db.String(64), nullable=False)
    state = db.Column(db.String(20), default='RUNNING', nullable=False)
    agent_revision = db.Column(db.Integer, nullable=False)
    knowledge_revision = db.Column(db.Integer, nullable=False)
    prompt_version = db.Column(db.String(64), nullable=False)
    model = db.Column(db.String(100), nullable=False)
    outcome = db.Column(db.JSON, default=dict, nullable=False)
    result = db.Column(db.Text, default='', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime)


class Settings(db.Model):
    """全局系统设置（key-value 键值对）"""
    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, default='')
    description = db.Column(db.String(200), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 默认设置值
_SETTINGS_DEFAULTS = {
    'smart_dedup_enabled': ('false', '智能去重开关'),
    'smart_dedup_threshold_minutes': ('60', '智能去重时间阈值（分钟）'),
}


def get_setting(key, default=None):
    """读取设置值，优先数据库 → 内置默认 → 参数默认"""
    row = Settings.query.filter_by(key=key).first()
    if row:
        return row.value
    builtin = _SETTINGS_DEFAULTS.get(key)
    return builtin[0] if builtin else (default or '')


def set_setting(key, value):
    """写入设置值（upsert）"""
    row = Settings.query.filter_by(key=key).first()
    if row:
        row.value = value
    else:
        desc = _SETTINGS_DEFAULTS.get(key, ('', ''))[1]
        row = Settings(key=key, value=value, description=desc)
        db.session.add(row)
    db.session.commit()
