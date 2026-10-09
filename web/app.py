from flask import Flask
from config import Config
from models import db


def create_app(telegram_manager=None):
    app = Flask(__name__, template_folder='templates')
    app.config['SQLALCHEMY_DATABASE_URI'] = Config.DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = Config.SECRET_KEY
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
    app.config['OPENAI_API_KEY'] = Config.OPENAI_API_KEY
    app.config['OPENAI_MODEL'] = Config.OPENAI_MODEL

    db.init_app(app)

    # 将 telegram_manager 注入到 app 上，供路由使用
    app.telegram_manager = telegram_manager

    with app.app_context():
        db.create_all()
        from models import AIDecision, AIExample, AIReplayRun
        AIDecision.query.filter_by(state='RUNNING').update({'state': 'UNCERTAIN', 'result': 'Процесс прерван. Проверьте Telegram; автоматического повтора нет'})
        AIReplayRun.query.filter_by(state='RUNNING').update({'state': 'ERROR', 'result': 'Тест прерван перезапуском. Автоматического повтора нет'})
        AIExample.query.filter_by(running=True).update({'running': False})
        from sqlalchemy import inspect, text
        if 'related_id' not in {column['name'] for column in inspect(db.engine).get_columns('ai_facts')}:
            db.session.execute(text('ALTER TABLE ai_facts ADD COLUMN related_id INTEGER'))
        if 'fallback_language' not in {column['name'] for column in inspect(db.engine).get_columns('ai_agents')}:
            db.session.execute(text("ALTER TABLE ai_agents ADD COLUMN fallback_language VARCHAR(30) NOT NULL DEFAULT 'AMHARIC'"))
        agent_columns = {column['name'] for column in inspect(db.engine).get_columns('ai_agents')}
        for name in ('delete_personal_data', 'delete_payment_data', 'delete_identity_documents'):
            if name not in agent_columns:
                db.session.execute(text(f'ALTER TABLE ai_agents ADD COLUMN {name} BOOLEAN NOT NULL DEFAULT 1'))
        for name, definition in {
            'semantic_memory': 'BOOLEAN NOT NULL DEFAULT 0',
            'memory_error': "TEXT NOT NULL DEFAULT ''", 'memory_retry_at': 'DATETIME',
            'allow_ban': 'BOOLEAN NOT NULL DEFAULT 0',
            'ban_confidence': 'FLOAT NOT NULL DEFAULT 0.98',
            'language_profile': "VARCHAR(20) NOT NULL DEFAULT 'ethiopia'",
            'custom_language': "VARCHAR(80) NOT NULL DEFAULT ''",
            'language_instructions': "TEXT NOT NULL DEFAULT ''",
            'glossary': "TEXT NOT NULL DEFAULT ''",
            'support_contact': "VARCHAR(200) NOT NULL DEFAULT 'support@betjam.com'",
            'support_text': "TEXT NOT NULL DEFAULT ''",
            'privacy_text': "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in agent_columns:
                db.session.execute(text(f'ALTER TABLE ai_agents ADD COLUMN {name} {definition}'))
        decision_columns = {column['name'] for column in inspect(db.engine).get_columns('ai_decisions')}
        for name, definition in {
            'rule_ids': "TEXT NOT NULL DEFAULT '[]'", 'training_needed': 'BOOLEAN NOT NULL DEFAULT 0',
            'training_status': "VARCHAR(20) NOT NULL DEFAULT 'OPEN'",
            'analysis_attempts': 'INTEGER NOT NULL DEFAULT 0',
            'next_analysis_at': 'DATETIME', 'analysis_started_at': 'DATETIME',
            'has_reply': 'BOOLEAN NOT NULL DEFAULT 1',
            'embedding': 'JSON', 'embedding_hash': "VARCHAR(64) DEFAULT ''",
            'memory_method': "VARCHAR(20) NOT NULL DEFAULT 'words'",
            'memory_example_ids': "JSON NOT NULL DEFAULT '[]'",
            'ban_status': "VARCHAR(20) NOT NULL DEFAULT ''", 'delete_status': "VARCHAR(20) NOT NULL DEFAULT ''",
        }.items():
            if name not in decision_columns:
                db.session.execute(text(f'ALTER TABLE ai_decisions ADD COLUMN {name} {definition}'))
        if 'training_needed' not in decision_columns:
            db.session.execute(text("UPDATE ai_decisions SET training_needed = 1 WHERE state = 'REVIEW' AND confidence > 0"))
        AIDecision.query.filter_by(state='UNCERTAIN', action='DELETE_BAN', ban_status='RUNNING').update({'ban_status': 'UNCERTAIN'})
        AIDecision.query.filter_by(state='UNCERTAIN', action='DELETE_BAN', delete_status='RUNNING').update({'delete_status': 'UNCERTAIN'})
        AIDecision.query.filter_by(state='UNCERTAIN', action='DELETE_BAN', delete_status='PENDING').update({'delete_status': 'SKIPPED'})
        from ai_queue import recover_analysis
        recover_analysis()
        example_columns = {column['name'] for column in inspect(db.engine).get_columns('ai_examples')}
        for name, definition in {'guidance': "TEXT NOT NULL DEFAULT ''", 'kind': "VARCHAR(20) NOT NULL DEFAULT 'test'",
                                 'embedding': 'JSON', 'embedding_hash': "VARCHAR(64) DEFAULT ''"}.items():
            if name not in example_columns:
                db.session.execute(text(f'ALTER TABLE ai_examples ADD COLUMN {name} {definition}'))
        task_columns = {column['name'] for column in inspect(db.engine).get_columns('scheduled_tasks')}
        for name, definition in {
            'once_at': 'DATETIME', 'send_immediately': 'BOOLEAN DEFAULT 0',
            'revision': 'INTEGER NOT NULL DEFAULT 0',
            'delivery_state': "VARCHAR(20) NOT NULL DEFAULT 'ready'",
            'completed_at': 'DATETIME', 'last_error': "TEXT DEFAULT ''",
            'caption_mode': "VARCHAR(20) NOT NULL DEFAULT 'fixed'",
            'caption_language': "VARCHAR(80) NOT NULL DEFAULT 'ru'",
            'caption_max_chars': 'INTEGER NOT NULL DEFAULT 300',
            'caption_use_image': 'BOOLEAN NOT NULL DEFAULT 0',
            'caption_instructions': "TEXT NOT NULL DEFAULT ''",
            'account_mode': "VARCHAR(20) NOT NULL DEFAULT 'single'",
            'last_account_id': 'INTEGER',
        }.items():
            if name not in task_columns:
                db.session.execute(text(f'ALTER TABLE scheduled_tasks ADD COLUMN {name} {definition}'))
        if 'caption' not in {column['name'] for column in inspect(db.engine).get_columns('task_images')}:
            db.session.execute(text('ALTER TABLE task_images ADD COLUMN caption TEXT'))
        if 'sent_by_account_id' not in {column['name'] for column in inspect(db.engine).get_columns('task_images')}:
            db.session.execute(text('ALTER TABLE task_images ADD COLUMN sent_by_account_id INTEGER'))
        if 'session_kind' not in {column['name'] for column in inspect(db.engine).get_columns('accounts')}:
            db.session.execute(text("ALTER TABLE accounts ADD COLUMN session_kind VARCHAR(20) DEFAULT 'telethon'"))
        for table in ('keywords', 'scheduled_tasks', 'pending_replies'):
            if 'image' not in {column['name'] for column in inspect(db.engine).get_columns(table)}:
                db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN image VARCHAR(100)'))
        db.session.commit()

    @app.get('/media/<name>')
    def media(name):
        from flask import send_from_directory
        from pathlib import Path
        return send_from_directory(Path(app.instance_path) / 'uploads', name)

    @app.context_processor
    def caption_context():
        import secrets
        from flask import session
        from captions import LANGUAGES
        session.setdefault('caption_csrf', secrets.token_urlsafe(32))
        return dict(caption_languages=LANGUAGES, caption_api_ready=bool(app.config['OPENAI_API_KEY']),
                    caption_csrf=session['caption_csrf'])

    @app.errorhandler(413)
    def too_large(error):
        return 'Файл слишком большой. Максимальный размер запроса: 64 МБ.', 413

    from web.routes.main import main_bp
    from web.routes.accounts import accounts_bp
    from web.routes.keywords import keywords_bp
    from web.routes.tasks import tasks_bp
    from web.routes.logs import logs_bp
    from web.routes.whitelist import whitelist_bp
    from web.routes.targets import targets_bp
    from web.routes.queue import queue_bp
    from web.routes.settings import settings_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(accounts_bp, url_prefix='/accounts')
    from web.routes.profile import profile_bp
    app.register_blueprint(profile_bp, url_prefix='/accounts')
    from web.routes.bulk_profile import bulk_profile_bp, init_bulk_profiles
    init_bulk_profiles(app)
    app.register_blueprint(bulk_profile_bp, url_prefix='/accounts')
    from web.routes.join import join_bp
    app.register_blueprint(join_bp, url_prefix='/accounts')
    app.register_blueprint(keywords_bp, url_prefix='/keywords')
    app.register_blueprint(tasks_bp, url_prefix='/tasks')
    app.register_blueprint(logs_bp, url_prefix='/logs')
    app.register_blueprint(whitelist_bp, url_prefix='/whitelist')
    app.register_blueprint(targets_bp, url_prefix='/targets')
    app.register_blueprint(queue_bp, url_prefix='/queue')
    app.register_blueprint(settings_bp, url_prefix='/settings')
    from web.routes.assistant import assistant_bp
    from web.routes import assistant_examples
    from web.routes import assistant_training
    app.register_blueprint(assistant_bp, url_prefix='/assistant')

    return app
