from flask import Flask
from config import Config
from models import db


def create_app(telegram_manager=None):
    app = Flask(__name__, template_folder='templates')
    app.config['SQLALCHEMY_DATABASE_URI'] = Config.DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = Config.SECRET_KEY
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

    db.init_app(app)

    # 将 telegram_manager 注入到 app 上，供路由使用
    app.telegram_manager = telegram_manager

    with app.app_context():
        db.create_all()
        from sqlalchemy import inspect, text
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
    app.register_blueprint(keywords_bp, url_prefix='/keywords')
    app.register_blueprint(tasks_bp, url_prefix='/tasks')
    app.register_blueprint(logs_bp, url_prefix='/logs')
    app.register_blueprint(whitelist_bp, url_prefix='/whitelist')
    app.register_blueprint(targets_bp, url_prefix='/targets')
    app.register_blueprint(queue_bp, url_prefix='/queue')
    app.register_blueprint(settings_bp, url_prefix='/settings')

    return app
